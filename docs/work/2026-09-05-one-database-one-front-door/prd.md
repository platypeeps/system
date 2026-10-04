---
title: one database holds the state, one screen is the front door
created: 2026-09-05
---

# PRD — one database, one front door

## Problem

The operator tracks work in seven places and reads none of them well.

| Surface | What it holds today | Who reads it |
|---|---|---|
| `docs/work/` frontmatter, per repository | item status | `sd-status`, by hand |
| `~/.cache/sd-ai-command-pack/index.sqlite` | 1,175 GitHub rows, marked a cache | `sd-status`, read-only |
| Obsidian vault, three databases | blog ideas, topics, skill proposals, each with a ladder | three crons, one of which names a routine that does not exist |
| Obsidian TaskNotes | 207 dated notes | a nightly digest email |
| a project decision register in another repository | numbered entries, a few of them open work | the operator, in one long file |
| Email | health check, brew doctor, drift, backup verify, dependabot, secret scan | nobody, reliably |
| Handoff packets | one, written 2026-09-02 | a start hook, once |

The dashboard that was meant to gather this, `local-project-dashboard` on port
8767, has zero GET requests and six logged errors. The operator is visual,
forgets commands and their options, and has 82 skill names to remember; the
front door is a terminal, and the terminal is where the framework got built
instead of used, at 327 framework commits to 62 product commits in eight days.

Cost is unmeasured. `local-agent-meter` appends `codexbar usage` per provider
every four hours into a blog piece's data file, and nothing reads it. The one
cost line the operator worries about, GitHub, is not in it.

Two things break the working day that no surface above touches. A killed
session loses its followups unless the operator remembers to write a packet,
and has once. A restarted `herdr` brings back its panes but not the agents in
them.

## Why the obvious fix is wrong

**Not another markdown file.** Every surface above started as one. A file
holds state only as long as one writer remembers it; seven writers produced
seven files. State needs a schema and one writer.

**Not GitHub issues.** They are external, they have been unstable, they expose
the operator's process in every repository they touch, and the operator has
already decided against them. Two are open across every repository the
operator owns. What GitHub holds for other people's repositories is mirrored
in, never written back.

**Not the whole control plane at once.** The dashboard could show crons,
services, ports, cost, assignments, skills, items, reports. It is the biggest
rabbit hole on the list, and the operator tinkers. It ships in sections, each
one reading one table, and the sections are named here so that nothing else
counts as this item.

**Not a bigger command line.** `sd today` exists for the agent. The operator
never has to type it.

## What this changes

One SQLite database owns the state. One library owns its schema and every
write. Two faces read it: `sd` for the agent, the dashboard for the operator.
Everything that produces state on this machine, sessions, crons, syncs, the
runner, writes through the library or not at all.

This is item B of three. Item A in `sd-ai-command-pack` holds the policy page,
the ship path, the review points, the skill paths, and every skill that will
write here. Item C in `writing-pack` moves ideas and pieces in after this
item's library exists. B's library is first in the order, because A's
requirements 5, 10, 11 and 12 and all of C write to it.

### Requirement 1 — the database and the library

`~/.local/share/sd/sd.db`, SQLite in WAL mode. Not a cache path; caches get
deleted.

A live WAL database is not a file a clone can copy consistently, and
`local-backup-verify` samples only files unchanged for seven days, so the
database itself is outside both. Backup is therefore its own step. A nightly
job, `sd-db-backup`, writes `VACUUM INTO` a dated directory under
`~/Documents/sd-backups/`, beside a copy of every file under
`~/.local/share/sd/` that is not the database, its journal or a worktree,
`providers.yaml` and `commands.yaml` today, since the registry's identity,
invocation and price fields and the palette's allow-list live in those
two files and nowhere in the database, and a restore that brings back the
rows and not the files brings back a runner that can dispatch nothing and
a palette that can run nothing; the directory sits in a path inside a pair
the clone already copies and
`local-backup-verify` already samples, rather than beside the live database
under `~/.local/share/sd/`, which no pair covers, supports explicit thirty-backup retention, and then
restores from the file
it just wrote: opens the copy, runs `PRAGMA integrity_check`, and compares
it with the source: the job writes a checkpoint row with the run's id into
the live database before the `VACUUM INTO`, and the restore passes when that
row is in the copy and every table's row count matches the source's at the
moment of the snapshot. Activity is not assumed; an empty database and one
with only old rows both pass on the checkpoint alone. The dated
directories never change, so the clone copies them and
`local-backup-verify` samples them under the `~/Documents` pair it has.
`sd-db-backup restore <date>` into an empty home puts the database and
the two files back, and is what a new machine runs after
`machine-setup.sh setup`. A restored database is a record, not a
permission: the world moved on after the snapshot, an `exec` row it
holds as `queued` may have run and done what it does, a merge it holds
as pending may have landed, and a cost it holds as reserved may have
settled, so a restore fails closed. It writes one `restore` row with the
snapshot's date and the restore's time; while that row is unreconciled
the runner dispatches nothing and the palette runs nothing, and Today says
so first. Source authority is reconciled against the checkout and never
restored from the snapshot, from B's round fifty: for every repository
whose restored row says `file` or `retiring` under `status_source`
while its checkout carries A's marker, and under `pieces_source` while
it carries C's, the restore sets `row` when the snapshot carries the
sitting's `verified` row for that repository and kind, the proof that
these rows are the ones the verify found equal to their lines, and
refuses that repository's recovery when it does not, from B's round
fifty-one, because the marker proves the checkout migrated and not
that this snapshot holds its data, and a nightly snapshot from before
the final import holds rehearsal rows: the row stays `retiring` with a
`restore` note naming the snapshot as older than the verify, every
writer refuses, and `sd restore reimport <repository>` imports the
lines from each row's `source_commit`, the parent of the commit that
added the marker for a row the default branch carried and a branch's
commit for one it did not, from B's round fifty-four, which git holds
as the verify saw them, verifies against the same hash and sets
`row`; a `retiring` row on a checkout without the
marker is a sitting that died, left for the sitting's rerun; and every
sitting takes a second snapshot after its commit, the cutover
snapshot, so that the ordinary restore is of the state after the
cutover and reconciles nothing. Every `queued` and `running` assignment in the snapshot is
marked `blocked` with a `restored` note, an `exec` row among them is
never requeued by anything but the operator's hand, a `merge` row
reconciles against GitHub as a requeued one does before it is offered
back, an assignment that carries `budget_usd` and is not `done` has
its budget marked spent by the restore, a `budget restored` note with
the number the snapshot shows, because the ledger under it predates
the snapshot and the money may be gone, so a requeue of it offers no
budget and takes a new one the operator types and the library refuses
a reservation against a budget so marked, from B's round forty-four,
and every bill with a cap or a plan is frozen until its month's
actual spend is read from the vendor's meter where one exists, MiniMax,
or typed by the operator from the vendor's console where none does, on
the Providers screen, so that a cap is not reopened by a snapshot that
predates what was spent against it. `sd restore resume` marks the row
reconciled, names what was blocked and what was frozen, and dispatch
resumes. Failures of the store itself never
depend on the store: a failed backup or restore, a database that cannot be
opened or written, and a report job that cannot write its row each send one
email through the path the cron jobs use today, and that path stays after
every other report has moved to rows, named with `attention` rows as the
two exceptions to requirement 5. Where the database is reachable the failure is also a
`report` item. A migration never
retires its source on the strength of a backup taken before it ran: the
source stays until a snapshot taken after the import restores and holds that
migration's rows, checked by count against what the import reported. The
migration command records the snapshot it was verified against, and the
retirement step refuses without one.

`local-sd-db/` in this repository holds the library, a Python package named
`sd_db`, installed into the pack's virtualenv and the dashboard's as a
built copy at a tagged version, `pip install` of this checkout at its
tag and never `-e`, from B's round forty-five, so that a branch switch
or a pull in this checkout changes nothing a running process imports
until the operator installs the next tag into both virtualenvs and
restarts what runs. It owns:

- the schema, as numbered migration files applied by one explicit
  command, `sd-db.sh migrate`, and never on open: on open the library
  reads the schema version and refuses a database newer than the
  version it was built for, naming both, and refuses to write one older,
  naming the command, so that an older process cannot run against a
  migrated database and a newer one cannot migrate it under the others;
  the operator runs `migrate` with the dashboard and the runner stopped,
  after a backup, and `machine-setup.sh doctor` reports the installed
  version in each virtualenv beside the schema version in the database
  and this checkout's tag, naming any of the three that differs;
- every write, as named functions; no caller issues SQL;
- the read queries the two faces share, so `sd today` and the today screen
  cannot disagree;
- the provider registry, read from `~/.local/share/sd/providers.yaml`, in the
  format item A fixes. The file holds identity: each provider's start line or
  URL, vendor, bill, reader, price, the `env` variables its process
  receives, and the two role lists. On first open it
  seeds the `provider` and `bill` tables, and from then on those rows hold
  what changes from the dashboard: enabled, reason, the order of each role
  list, and a bill's cap. The library merges file and rows on every read, so
  `sd-review` in the pack sees one registry.

Every status write also writes one `note` row of kind `status_change`, the
old status, the new one and the time, in the same transaction, by the
library and by nothing else, so that an item's row holds its current
status and its notes hold the history: the item screen shows it in order,
the age histogram reads a row's status timestamp from the latest one, and
lead time, the days from `planning` to `done`, and the days spent
`blocked` are sums over them. Asked for on 2026-09-05; cheap on day one
and unrecoverable later.

Day-one tables, eleven:

| Table | Holds |
|---|---|
| `repo` | path, remote, detected mode, `merge_policy` (default `manual`), `status_source` (`file` until the `docs/work` sitting, `retiring` while it runs, `row` after it), and `pieces_source` with the same three values for C's pieces, owned by C's sitting and read by nothing in this item but the restore, from C's round six |
| `item` | id, kind (`work`, `idea`, `task`, `report`, `proposal`, `skill-review`, `dep`), repo, branch, path to its artifacts, title, status, `stage` (the kind's own word, where it has one), priority, due, source, external id, `shipped_at`, timestamps; `fields`, the declared fields of a kind a pack's manifest describes, and `body`, its sections, for `idea` and the writing pack's `topic`, from C's round thirteen |
| `note` | item, timestamp, kind (`followup`, `decision`, `proposal`, `question`, `comment`, `exec`), body, session, `resolved_at`; an `exec` note also carries started, ended, exit code and the output path |
| `shadow` | the current `issue` table: tracker, repo, url, number, kind, title, state, author, first and last seen |
| `assignment` | item, role, provider, status, started, ended, cost, result, `after` (an assignment id or none), `parent` (the row that spawned this one, or none), `lane` (`serial` or `parallel`), `budget_minutes`, `phase` (a `merge` row's last completed side effect) |
| `skill_use` | timestamp, skill, surface, mode (`direct`, `path`), cwd |
| `trial` | skill, started, expires |
| `cost` | call id, timestamp, provider, bill, role, repo, assignment, pass, owner pid, tokens in, tokens out, usd, `window_minutes` and `used_percent` for a `meter` row, source (`reserved`, `sending`, `run`, `bound` or `meter`) |
| `provider` | name, enabled, reason, author rank, reviewer rank, seeded from the registry |
| `bill` | name, cost basis, `cap_usd_month`, seeded from the registry |
| `state` | the operational records that are not items, from B's round thirty-five: kind, key, timestamp, body, `resolved_at`. Kinds: `checkpoint`, the backup run's id written before the snapshot; `verified`, a sitting's verify, the repository, the kind and the content hash it found equal, written before the sitting's snapshot, from B's round fifty-one; `restore`, the snapshot's date and the restore's time, `resolved_at` set by `sd restore resume`; `watermark`, the tracker, repository and cursor `sd shadow sync` resumes from; `heartbeat`, the runner's pid, host and last tick, item D |

The item status vocabulary is `planning`, `ready`, `in_progress`,
`ready_to_send`, `blocked`, `done`. The schema grows by migration file,
a column or a table, reviewed like any change; what it does not do is
grow by convention, a record kind written into a table whose columns
were named for something else, which is why the operational records
have `state` and the earlier rule that froze the count at ten is
withdrawn, from B's round thirty-five.

Status is not free metadata, from B's round thirty-five. The library's
one status function, `transition`, takes the item, the target and who
asks, and applies the kind's table. For a `work` item, `done` is written
by delivery, `sd-ship` or item D's runner on a confirmed merge, item A's
requirement 5, and by `cancel`, a separate action that writes `done`
with a `cancelled` note carrying the reason, and by nothing else: a
`done` asked for from the item screen, `sd task
status` or the palette's status entry on a work item with no confirmed merge is refused
with "work completion requires verified delivery or cancellation evidence" (amended 2026-09-17, approved by the owner 2026-09-17 (note 2706): this said "from the board, a bulk action, the item screen, `sd status` or the palette ... naming the merge and offering cancel"; the sentence is the one `source:local-sd-db/sd_db/workflow.py::change_status` raises, clause 7.17, the verb is `sd task status`, and the board and a bulk action carry no status write), and the card snaps back. A work
item's other moves, `planning` to `ready` to `in_progress` and back,
`blocked` from and to any of them, are the operator's, and `ready_to_send`
is written by `sd-ship` when the pull request is open, by the operator
from `in_progress` when they opened one by hand, and back to
`in_progress` by either. An item with a `queued` or `running` assignment
refuses every status write from a page or a command, naming the row,
because the runner's lifecycle writes own it and two writers on one row
is the collision this item exists to remove. The way out never needs
the runner, from B's round thirty-seven: a `queued` row has no process,
so the library's own `cancel` on an assignment, from the item screen,
the runner board and `sd assignments cancel` / `sd runner cancel` (the pack's existing verbs, owner decision (a) of 2026-09-16, in place of a built `sd assign cancel`), ends it `cancelled` with a
`cancelled by <who>` note, the caller's name, in the cancel's own write and frees the item for its next status write, a separate one, with no process, sleep or heartbeat read (amended 2026-09-17, approved by the owner 2026-09-17 (note 2706): this line said `blocked` with `cancelled by operator` "in one transaction"; the cancel is one write and the item's next status write another, two in #431's measurement; the page now follows the library's terminal status, `cancelled`, guarded in `source:local-sd-db/sd_db/writes.py::update_assignment`, and the note the cancel writes, `cancelled by <who>` with the caller's value: `dashboard` from the item screen, the login from the pack's verbs, `operator` in the fixtures; the runner cancel writes it to the row's `result`, `cancel_assignment` to an item note),
whether the runner is loaded, asleep, or not yet built; and the refusal
on a `queued` row names that cancel. A `running` row has a process, and
ending it is the runner's control entry, requirement 5, which the
refusal names instead. So the fourth slice of the landing order, which
creates assignments before the fifth runs them, leaves the operator
able to assign, see the row wait, and take it back. An `idea` moves by its ladder, item C; a
`report` moves `planning` to `done` by resolve; a `task`, a `proposal`, a
`skill-review` and a `dep` move freely. Every refusal says which rule
and what would satisfy it.

The `status:` line leaves a work item's `prd.md`, by the operator's decision
on 2026-09-05 after A's round thirty-three: requirement 2's `docs/work`
migration reads every item's line outside the archive into its row, `done`
ones as unmarked `done` rows that the retire step's own pull request marks
with `Closes:`, from A's round thirty-four, and removes it from the file in
the retire step's one commit, after item A's reader and ship path have
landed and never before, from A's round thirty-six; until that sitting
the line is the record and every writer still writes it, the rows are a
rehearsal, and A's reader reads the line while the repository's row says
`status_source: file`, so that a status changed between the slices is
never shadowed by a stale row, and the sitting's final import and verify
carry the change into the row and set `row`, after which a line anywhere
is stale and ignored and the old writers refuse, from A's rounds
thirty-seven and thirty-eight, and `row` is set before the lines are
removed and never after, from C's round five, so that a sitting that
dies between two of its steps is rerun, each step idempotent, and the
lines a rerun finds are ones every reader ignores; the sitting's first transaction sets
`retiring`, and that is the freeze for `docs/work`, from B's round
forty-nine: every status writer reads `status_source` in the
transaction that writes and refuses under `retiring` naming the
sitting, because a note that a source is read-only stops no session
already running and no other checkout of the repository, and the
database's one write lock does, a writer holding it finishing its line
before the sitting's import reads, and one that comes after refusing;
a verify difference sets `file` back and the refused writer succeeds on
its retry; a writer on another machine, or one under a pack older than
A's reader, is fenced by git, its line conflicting with the removal at
its merge and the lint refusing it after; and the register has one
writer, the operator's hand, who is running the sitting. From the
switch a status change
is one write, to the row, in any
checkout and in any state of the branch. No mirror, no `rev`, no guard, no
refresh and no closure: rounds eleven to thirty-two of A's review kept the
line as a derived mirror, and each round from thirty to thirty-three found
the next case its guard or its closure got wrong, for a word git already
holds. A checkout with no database, CI among them, asks `sd_lib.delivered`
whether an item is delivered, from the `Delivers:` and `Closes:` trailers
item A's requirement 5 defines, and asks nothing else; `done` reaches git
through the delivering merge's own message, through the next pull request
`sd-ship` opens in that repository, whose body and merge message carry
`Closes:` for a `done` row the default branch does not yet mark, and
through one empty commit on the item's own branch
when the triad never left it or lives on a guest fork, from A's rounds
twenty-eight to thirty-three, and no pull request and no file write is
ever made for a status. No
directory is deleted, by the operator's decision on 2026-09-05.

### Requirement 2 — the migrations that fill it

Each migration is one command, idempotent, and reports counts, and each
runs in the same order: freeze, import, verify, retire. Freeze first: every
writer to the source is stopped before the final import, the vault jobs
unloaded, the index refresh disabled, for the register a note on the item
that it is read-only until the switch, and for `docs/work` the `retiring`
value on the repository's row, which every status writer refuses under,
from B's round forty-nine. Then the final
import, reporting counts. Then the migration compares the frozen source
with the rows by identity and content, not by count alone, and names every
difference. Then the post-import snapshot restores and holds those rows.
Only then does the source retire and the database become the authority for
that kind. A migration run before the freeze is a rehearsal and retires
nothing. The freeze is minutes, not a slice, from B's round thirty-three:
it is taken only when the writer and the reader that replace the source
are already landed, so that freeze, final import, verify, snapshot and
retire are one command run in one sitting and the source is read-only
for as long as that run takes, the import and the verify of a source
that lives in git read committed trees and never a working copy,
refusing while `git status --porcelain` names anything under it, from
C's round eight, and for `docs/work` every branch of the remote and
not the checkout's `HEAD` alone, from B's round fifty-four, because an
item lives on its branch until its merge and a branch can carry an
item the default has not seen or a line newer than the default's: the
sitting fetches, reads the item's line from every branch that carries
the item, refuses naming the item and the branches when two lines
disagree, for the operator to reconcile and rerun, and records on
each row the branch and commit its line came from, `source_commit`,
which `sd restore reimport` reads from in place of the marker
commit's parent, that parent being the default's tree alone, and
a marker whose one line is a commit, C's from C's round thirty-two,
names the commit the reimport reads a source's lines from, since a
snapshot older than the verify carries no row that could, and a
commit between the retire and the marker commit's puts a tree the
verify never saw under the marker; the
verify writes its `verified` row,
repository, kind and content hash, before the snapshot so that the
snapshot carries the proof, from B's round fifty-one, and a second
snapshot follows the retire,
the cutover snapshot, from B's round fifty; a run whose verify names a difference
lifts the freeze and retires nothing, and the source stays authoritative
with every writer it had. Until that run, the import is additive: the
rows exist beside the source, the dashboard reads them, and the commands
that write the source keep writing it, so no kind of work is untracked
between slices.

- `index.sqlite` to `shadow`: 1,175 rows. The pack's `dashboard` package
  holds the only collector that refreshes those rows from GitHub, and a
  one-time import would leave `shadow` frozen on the day of the switch. So
  the collector moves into the library first, as `sd shadow sync`, keeping
  the `tracker_watermark` incremental fetch, and a nightly job in this
  repository runs it. Only when a sync after the import has brought in a new
  issue and a changed state, asserted by a test, is `index.sqlite` deleted,
  `sd-status` switched to `shadow`, and the pack's `dashboard` package and
  `bin/sd-dashboard` retired.
- Every `docs/work/*/prd.md` in a registered repository to `item` rows,
  enumerated from the filesystem across every repository the `repo` table
  holds that has the directory, not from a list kept in a document. The
  `repo` table is the bound, from B's round forty-eight: "on the machine"
  was unbounded, and item D's runner clones a registered repository's whole
  working tree onto the work volume, so a clone's own `docs/work/*/prd.md`
  files are on the machine, carry no `item` row, and would fail criterion 6
  for as long as the retention holds the clone. A path under the worktrees
  directory is not a registered repository and is never enumerated.
  Today, counted on 2026-09-05, they span six repositories, and most of them
  sit in one repository whose items were created more than forty-five days
  before the count, so the first sweep after the migration offers them
  together; the migration seeds each row's idle clock from the
  file rather than from the import, so a long-idle item reads as long-idle
  and not as newly touched.
- The decision register's open entries to `item`
  rows with `repo` set; the register keeps its decisions and links to the rows.
- The vault's Blog Ideas and Topics databases to `item` rows of kind `idea`.
  The ladder word is kept verbatim in `stage`, and `status` is derived by one
  declared table, total over both ladders in the writing manifest, which
  item C owns, `2026-09-05-the-writing-pipeline-runs-on-the-row` in
  `writing-pack`: for Blog Ideas, `inbox` to `planning`, `accepted` to
  `ready`, `drafting` to `in_progress`, `published` to `done` with
  `shipped_at` from the note, `declined` to `done` with a `declined` note and
  no `shipped_at`; for Topics, `candidate` to `planning`, `active` to
  `in_progress`, `parked` to `planning` with `parked_at`, `retired` to
  `done`, from C's creation, which found the first table naming the tips
  ladder's `ready` and `approved`, words no Blog Ideas or Topics note
  carries, and no Topics word at all, so that the Topics import would have
  refused all eleven. The import refuses a stage the table does not name.
  The 95 `declined` ideas are therefore closed, not backlog. Nothing consumes
  `stage` in this item: `blog-idea-accept` stops as the freeze, and item C
  defines what advances a piece from one stage to the next, on the row, and
  puts the two kinds on the database as `sd store`'s driver for them, so
  that `sdw-ideate` and the pack's `sd-brief` keep their calls.
- TaskNotes: not in this item. The 207 notes stay in the vault with their
  nightly job until the operator retires them; the migration command exists
  and is run only on that instruction.
- The open GitHub issues the operator authored to `shadow` rows,
  not to `item` rows, and none of them closes on GitHub, from the operator's
  decision of 2026-09-05. One of them is in a repository other people can
  read: an issue closed with a pointer to a database only the
  operator can reach would strand every other reader, so the issue stays open
  and `sd shadow sync` keeps its state. The sync's configuration names the
  repositories it fetches, and the operator's
  classic token carries `repo` scope, which reaches them.
- The vault's Skill Proposals database is not migrated. Its ten rows are
  history; item A retires the kind, and `sd_db/sources/vault.py` drops the four
  ladder rows that answered for it. The two cuts land in different repositories
  and are **ordered**: criterion 5 asserts manifest -> table, so the table may
  lose the rows only after the manifest has lost the kind. See C-177.

### Requirement 3 — Obsidian leaves the process

Obsidian stays as a knowledge base: tips, documentation, reference notes. The
pack reads no state from it and runs no process through it.

The scheduled jobs that read or write vault process state stop before the
final import of their rows, as requirement 2's freeze: `obsidian-review-daily`,
`blog-idea-accept`, `tips-accept`, `tips-weekly`. Each is unloaded and its
job file removed. One comes back only when a dashboard section consumes its
output as rows, and then it writes rows, not notes and not email.

**Amended 2026-09-12, operator decision.** Two of the six jobs this
requirement first named are vault maintenance and not process, and stay:
`vault-cleanup` (normalisation, the drift report, and the vault's daily git
commit — the knowledge base's own backup) and `vault-map` (the watchdog that
`vault-cleanup` ran). Neither reads a ladder or writes a queue. The four
process jobs above do not stop under this item either: hand-off 2 keeps the
vault the writer for the vault kinds until item C
(`2026-09-05-the-writing-pipeline-runs-on-the-row`, sd:232) lands its
replacement, and stopping the review digest, the accept steps and the weekly
sweep before then would end those flows with nothing consuming them as rows.
Their removal moves to item C's landing order, in the same change that removes
the vault's views, routine and queues. Criterion 8 reads accordingly.

TaskNotes and `obsidian-tasks-nightly` stay, by the operator's decision on
2026-09-05: the vault keeps its tasks and its nightly digest until the new
system has proven itself, and the operator removes them explicitly then. New
tasks may be filed as `task` items meanwhile; the two lists run side by
side, and nothing in this item reads or writes the vault's.

The 104 Codex sessions that ran from the vault in eight days are not these
jobs. They are the writing pipeline's hostile-read fan-out, one `codex exec`
per piece through `local-adversarial-gate`. Item A's review caps govern them;
nothing here touches them.

### Requirement 4 — the runner is its own item

The runner, `local-sd-runner/`, is specified and built in item D,
`docs/work/archive/2026-09/2026-09-05-the-runner-works-the-queue/`, split from this item
on 2026-09-05 by the operator's decision: twenty of this item's first
forty-eight findings landed on the runner, and rounds twenty to
twenty-six still found a high-severity defect there at about one and a
half per round, so the next ten are cheaper to find in a few hundred
lines of runner code against the fixture harness, requirement 10, than in
thirty-minute rounds over prose. This item keeps what the runner writes
through and reads from: the `assignment` table and its vocabulary, status
`queued`, `running`, `blocked`, `done`, role `author`, `reviewer`, `merge`,
`exec`, `lane`, `after`, `parent`, `phase` with the values `updated`,
`ci_passed`, `merged`, `budget_minutes`, and the
pids and
start time the runner claims a row with; the runner's `heartbeat` row in
`state`;
`merge_policy` on the repository row; the worktree, kept-worktree and
bundle paths the backbone backs up; and the library calls that create
assignments, batches and `exec` rows. Every screen that shows a row of
those kinds is this item's. The runner lands last, in the landing order
below, and until it exists an assignment row is created and shown but
dispatched by nothing, which Today says.

### Requirement 5 — the dashboard is the front door

`local-project-dashboard/` is rebuilt on the library. Five sections, in this
order of delivery. Every action is a button, and beside every button is the
command that does the same thing, so the operator learns by seeing.

1. **Today.** Items due or in progress, finished-unsent items first with
   days-since-ready, open followups, the cost tile, the four weekly numbers,
   the missing-trailer count, and, while any assignment is queued or
   running, the runner board: one lane per assignment status, `queued`,
   `running`, `blocked`, and `done` for the day, a card per row with its
   item, repository and provider and, when it waits, what it waits on, the
   `after` row, the repository another row holds, or the operator's merge,
   so that a collision or a stall is a position on the board and not a
   sentence in a log. Below the board, the day's timeline: one lane per
   repository, one bar per assignment from `started` to `ended` or now, a
   wait drawn hatched with what it waited on, and the row's
   `budget_minutes` as a mark on the bar, so that what ran, what waited and
   what overran is read from position; a bar opens the row with requeue,
   discard and the budget beside it. The Usage screen shows the same
   timeline for the week. The cost tile is one line per bill, spend
   this month against cap where one exists, and opens the usage report. This
   is also what `sd today` prints.
2. **Backlog.** Every open item, filtered by kind, repo, status, in one of
   three views of the same rows, chosen by a toggle the URL carries, asked
   for on 2026-09-05. A list. A board, one column per status word,
   `planning`, `ready`, `in_progress`, `ready_to_send`, `blocked`, and
   `done` for the week, where a card dragged to a column is the
   status-change write through the library's `transition`, so a work item
   dragged to `done` with no confirmed merge snaps back with the reason
   and cancel beside it, and the `blocked` and `ready_to_send` columns show
   a stall as a place rather than a word. A matrix, four quadrants by
   urgent and important, where important is the row's `priority` and
   urgent is derived, `due` within seven days, `ready_to_send` older than
   three days, or a `report` row carrying `attention`, so that important
   work with no due date has a quadrant of its own instead of sinking
   under what is due; the matrix is a picker, and past thirty cards the
   filter comes first. The filter, the selection and the bulk actions are
   the same in all three, from the one list component, so `Run sequential`
   runs from a column or a quadrant as it does from the list. Above every
   view, the age histogram: open items by days in their current status,
   from the row's status timestamp, with `ready_to_send` as its own series,
   so that what the operator is sitting on is one glance; a bar is a
   filter on the list. Writes: status change, priority, assign to an agent
   (creates an `assignment` row).
3. **Item.** One item: its artifacts rendered from git, its notes in order, its
   assignments and their cost, its shadow if it has one. Writes: status, notes,
   assign, review now with a provider picked from the registry table, and
   `merge_policy` on its repository.
4. **Skills.** The catalog: what each skill does and when to use it, use per
   surface with `direct` and `path` separated, trials with expiry, and
   whether it is installed on a path, on trial, or in `contrib/`. Writes:
   promote and demote, which call the library to open the pull request in the
   pack; the operator merges it. Run-with-agent creates an assignment.
   Review, on any skill in any of the three states, opens a `skill-review`
   item on the pack's repo row and runs one reviewer pass with the
   skill-review lens: internal consistency and correctness of the skill text,
   its steps, flags and references against the binaries and files they name;
   and fit with the installed set, overlap, contradiction, a handoff no other
   skill picks up, vocabulary that drifts from the rest. Each recommendation
   lands as a `proposal` note naming the file and lines it changes. The
   operator accepts one or many; apply creates one assignment whose brief is
   the accepted proposals, the runner edits the skill in its worktree of the
   pack and ships one pull request, and the operator merges. `sd skill review
   <name>` runs the same pass from the terminal. The reviewer is a different
   vendor from the author that wrote the skill's last change, by the same
   rule as every other review.
5. **System.** Collapsed by default. Daemons, ports, the existing status views,
   and every report that arrives by email today as `report` items with assign.
   When a job's report has a row, its email keeps going: a stored row is not a
   delivered report while the dashboard has no reader, and the problem
   statement counts zero GET requests. The mail path is retired job by job,
   in one commit the operator asks for by name once satisfied that the new
   framework works, and never on its own; the adoption gate in criterion 12
   is the earliest the pack accepts that ask. After that, a `report` row that
   carries `attention`, the job found something to act on, a failed backup
   verify, a secret, a drift, still sends one email through the cron path,
   as the second named exception to this section, so that a report which
   needs a hand reaches one whether or not the dashboard was opened that
   week. The same row pushes, and neither the mail nor the push is
   trusted to the moment of writing, from B's round forty-two: the row
   is written with `attention` and two pending deliveries, `mail` and
   `push`, in the one transaction, and a sender, run by the writing job
   right after its commit, by every report job at its start and by the
   dashboard process on its minute tick, claims each delivery still
   pending in one transaction, `sending` with its pid and a lease of a
   minute, sends it, and marks it sent with the time on the row, so
   that two senders on one tick send it once, the second finding it
   claimed, from B's round fifty-three, the same shape as a cost
   reservation's `sending`; a claim whose owner is dead or whose lease
   has passed is retaken, so that a
   process that died between the commit and the send, or inside the
   send, leaves a row the
   next sender finds, and a row is delivered only when both marks are on
   it. The push goes through `local-notify`'s ntfy channel with the
   row's title and a link to it, so that a failed backup or a found
   secret reaches the phone and not only the inbox, asked for on
   2026-09-05; a plain `report` row pushes nothing and carries no
   pending delivery. Two screens of its own:
   - **Providers and bills.** The registry as a table: name, vendor, bill,
     enabled with its reason, rank in each role list, spend this month against
     the bill's cap, and the scorecard for the month, from `cost` rows and
     the review notes: passes run, blocking findings raised, dollars per
     pass, and fallthrough hits, the calls that skipped this entry for rate
     limit or a window at zero; the scorecard sits beside the rank because
     it is what the rank is decided on. Writes: enable, disable, move up or
     down in the `author` or `reviewer` list, and raise or lower a cap in
     one action with the new number typed beside the old one. Every write is a `provider` or `bill`
     row; the file is never edited from the page.
   - **Usage.** Which vendor was used how much: per vendor, provider, bill and
     role, the passes, tokens in and out, and dollars, by week and by month,
     with a per-repository breakdown. Subscription providers show tokens and
     the plan usage `local-agent-meter` reads; prepaid and capped bills show
     dollars against balance or cap, and each bill with a cap draws its burn:
     spend to date as a line across the month, the cap as a rule, and a
     straight projection from the month's rate to month end, so that a cap
     the month will pass is seen before it is passed, with raise and lower
     beside it; a `plan` bill draws its windows instead, the five-hour and
     the weekly remaining as two gauges from the meter. The week's runner
     timeline sits below. `sd usage` prints the month.
   - **Command log.** Every command the palette ran, newest first, for as
     long as the database holds it: when, from which device and login, which
     allow-list entry with its placeholder values, the item it ran for, the
     repository and directory, the exit code, how long it took, and the
     captured output opened in place. Filters by entry, item, device, exit
     code, and day. Beside it, the allow-list as the server currently holds
     it, with the time it was last reloaded, so the operator reads what may
     run next to what did. Nothing here is deleted from the page; a row
     leaves only with its item. Captured output lives under
     `~/.local/share/sd/exec/`, one file per run, kept ninety days; the row
     keeps the exit code after the file is gone, and the prune of
     requirement 9 removes the file and marks the note `output expired`,
     never the note. `sd exec-log` prints the day.
   - **Dependencies.** By the operator's decision on 2026-09-05. The
     morning job `dependabot-daily` keeps every rule in
     `local-dependabot/ROUTINE.md`, the two gates, the four conditions, the
     widest-member rule, serial merges per repository, and stops writing a
     log for nobody: each run writes one `dep` item per open Dependabot
     pull request it saw, on the repository's row, with the pull request
     number, the packages and the widest bump, the class, the checks as
     proven, the merge state, the age, and the reason it is held, and one
     `report` item for the run that names every repository skipped and the
     gate that skipped it. A `dep` item's stage is `held`, `merged`,
     `closed` or `ignored`; the job moves what it merges to `merged` with
     `shipped_at`, and a row whose pull request has gone from GitHub closes.
     The list has the three controls every list has, filtering on
     repository, class, reason and age, and its bulk actions on a selection
     are merge, close, ignore this major, rebase, snooze and assign. There
     is one merge authority per repository, the runner's `merge` row once
     the runner exists, and a
     dependency merge is not a second one: merge, from the selection or
     from the morning job's safe class, creates one `merge` row per pull
     request on its `dep` item, and the runner takes each alone in its
     repository in queue order, ascending pull request number within a
     batch, and re-checks at that moment what its authority requires,
     from B's round forty-one: the row carries who authorised it,
     `routine` from the morning job's safe class or `operator` from the
     selection, and a `routine` row re-checks all four conditions of
     `ROUTINE.md` while an `operator` row re-checks the two that make a
     merge mechanically safe, the author is Dependabot and not a draft
     and GitHub says `CLEAN` on the head the operator saw, plus that
     every check that ran passed, and skips the two the tap decided,
     the bump's size and whether CI exists, since a major or a
     repository with no CI is exactly what the list holds for the
     operator to decide; a failing check refuses either row, and an
     employer repository gets no row from either, the owner gate; then
     merges through the GitHub API naming the head it checked, so that a
     rebase the bot performed in between, asked for from this page or by
     the runner itself, refuses the merge and the row re-checks the new
     head, comments `@dependabot rebase` on the siblings the merge made
     stale, and reports the count. So the morning
     job classifies and enqueues, and merges nothing itself, from the
     cutover on and not before, from B's round forty-three: the runner
     lands in the fifth slice after a spike, and the safe patches that
     merge on their own today must not stop merging for however long
     that takes, so until then the job keeps the serial merger it has,
     writing its `dep` rows beside it and moving what it merges to
     `merged` itself, and it is the one merge authority for dependency
     pull requests while it is; the cutover is one commit in the fifth
     slice, after D's end-to-end merge check has passed, that removes
     the job's merger and lets the runner take `merge` rows on `dep`
     items in the same change, so the two never merge at once and no
     morning passes with neither. Until the cutover, a merge from the
     selection is refused naming the slice. The job's
     classification still fans out per repository, read-only. Close, ignore
     and rebase are comments and closures that move no branch, and go
     through the library to the API directly, the way promote does; nothing
     here touches a checkout.
     Ignore this major posts Dependabot's own `@dependabot ignore this
     major version` and moves the row to `ignored`, so the bot stops
     reopening it. Assign is for a bump that needs code work, a pip bump
     that needs `make lock`: it opens a `work` item on the repository with
     one `author` assignment whose brief is the pull request, and the runner
     opens its own pull request with the fix and closes the bot's; nobody
     edits a bot branch. What stays human stays human: a major, a Docker
     digest, a repository with no CI. The list makes each a one-tap
     decision instead of a line in an unread email, and the tap is the
     authority the runner honours; an employer repository is the owner
     gate's and gets no row and no tap.
     Today shows the held count by class, the oldest held age, the last
     run and a missed run, a run that did not happen by the schedule in its
     job file, shown as the alarm the way a missing backup checkpoint is.
     The repository row shows its open bot pull requests, whether
     `dependabot.yml` exists, whether CI exists, and whether the default
     branch is protected, so a repository that collects bumps nobody can
     prove safe is a configuration decision shown as one. Beside the last
     is one action, `protect main`, on a repository the operator owns: pull
     requests only, CI required, branches up to date before they merge, no
     required approvals, written through
     the library to the API, by the operator's decision on 2026-09-05. The
     tap is the owner asking, which is the one case in which the pack
     changes a repository setting; on a repository the operator does not
     own the action is absent. `sd deps` prints the list. The pack's `sd-deps` skill goes with
     this, on item A: one rule set, in `ROUTINE.md`.

The dashboard writes only through the library. It never touches git or GitHub
directly. No graphs in this item.

The operator opens it mostly from an iPad, over Tailscale, and the browser
must see an origin it trusts: Fetch Metadata headers and `SameSite` cookies
are sent only to a potentially trustworthy URL, so a plain HTTP tailnet
address would render every page and refuse every write. The front door is
therefore `https://<this-mac>.<tailnet>.ts.net`, terminated by Tailscale
Serve with the certificate Tailscale issues for that name, reachable from
the tailnet and nowhere else. Serve proxies to the application, which binds
loopback on one port and refuses to start unless `tailscale serve status`
shows the HTTPS route to that port. Identity comes from the tailnet, not
from a password: Serve stamps every proxied request with
`Tailscale-User-Login`, the application serves only the operator's login,
and it reads that header only from a loopback peer. A process on this Mac
can reach the loopback port; on a single-user machine that process is the
operator's own, and the origin and token checks below still apply to it.
Every write needs a same-origin request, checked on `Sec-Fetch-Site` and
`Origin`, so a page open in the same browser cannot post a write. The Mac's
own browser uses the same HTTPS name. The Mac's terminal keeps `sd today`
and needs none of this.

Everything the dashboard renders that it did not write is untrusted, and
it renders a great deal it did not write on the same origin as the
controls above: artifacts from git, titles the migrations imported, notes a
session wrote, command output, pull request titles GitHub returned. One
rule, from B's round twenty-nine: escaped text by default, everywhere.
Markdown, where an artifact or a note is rendered as such, goes through
a sanitizer that emits no executable HTML, no `script`, no event-handler
attribute, no `javascript:` or `data:` URL, no raw HTML passed through,
and every link carries `rel="noopener"`; command output is text inside
`pre` and nothing else. Behind the filter stands a policy the browser
enforces whatever slipped it: every response carries
`Content-Security-Policy: default-src 'self'; script-src 'self';
object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors
'none'` and `X-Frame-Options: DENY`, the dashboard's one JavaScript
file is the only script, and no template holds an inline script or
handler, so a tag that reached the page runs nothing. The last
directive and the older header beside it are there for a different
attack, from B's round thirty-nine: the origin check and the execution
token prove that a request came from a dashboard page, not that the
operator meant the click, and a page on another origin of the same
tailnet site, which `SameSite=Strict` does not keep the cookie from,
could frame the dashboard under its own overlay so that a genuine
click landed on a real control. No page may embed the dashboard, so
no such overlay exists, and the check is a header the browser
enforces, not a script. The rendered artifact is not moved to a second
origin, because one origin is the design and the rules above make it
hold.

The iPad is the reference device, by the operator's decision on
2026-09-05: every screen is laid out for its width, portrait and
landscape, with touch targets a finger hits, and nothing on any screen
needs a hover or a horizontal scroll. The iPhone is second. Today, the
Backlog list, the Item screen and the palette work at its width, since
those are what a phone is opened for; the board, the matrix and the
charts render read-only at that width or fall back to the list, and a
phone layout for them is postponed until the iPad is done, the operator
having said the phone matters less and is not worth friction now.

The palette executes command lines on this Mac, by the operator's decision
on 2026-09-05 with the reach above in view. The guards, all of them, every
time:

- An allow-list, and nothing outside it. `~/.local/share/sd/commands.yaml`
  names every command the palette may run: a name, the argument vector, the
  screens it appears on, and typed placeholders where a value comes from the
  page, `item` for an id that must exist in `item`, `provider` for a name
  that must resolve in the registry. The page enumerates the palette from
  this file; a request names an entry and supplies placeholder values, never
  a string. Free text is refused, a value that fails its type is refused, and
  an entry absent from the file does not exist. There is no shell: the server
  starts the argument vector directly, never through `sh -c`. The System
  screen shows the list; the file is edited by hand and the server reloads it
  on change.
- Identity and origin as above, plus a session token. The server issues it
  on the first request that carries the operator's `Tailscale-User-Login`,
  bound to that login, random, and rotated daily; the cookie carries
  `Secure`, which the HTTPS front door makes possible, `HttpOnly` and
  `SameSite=Strict`, and no script reads it. What a script sends is a
  second value, the execution token, from B's round thirty-six: derived
  from the session by a keyed hash the server alone can compute, rendered
  into every server-rendered page as one `meta` element, and repeated in
  a request header on every execution; the server recomputes it from the
  cookie's session and the two must match, so a request forged from
  another origin has the cookie the browser attaches and not the token
  the page carried. A request without the cookie, without the header,
  with a header that is not the cookie's session's token, with a token
  rendered for another login's session, or with one from before the
  daily rotation, is refused, the last naming a reload.
- Every entry carries `mutates: true` or `false`, and a file with an entry
  missing it is rejected at load naming the entry. A read-only entry runs
  from the palette's own process, as the operator, in the item's worktree
  when the runner has one and otherwise in the repository's checkout, with
  output streamed to the page and kept with the record. A mutating entry
  never runs from the palette's process and never in the operator's checkout:
  the request creates an `assignment` row of role `exec` for the item, and
  the runner executes it in the item's worktree under the same
  per-repository serialization as every other assignment, item D; the
  page shows the assignment and streams its output from there. An entry on
  the System screen that mutates runs the same way against the standing
  `system` item's repository. A mutating entry for an item that has no
  branch is refused naming the item, because there is no worktree to run it
  in. An entry also carries `scope`, one of three. `worktree`, the default
  above. `supervisor`, for the commands that act on an assignment's
  worktree rather than in one, `sd worktree discard`, `sd worktree
  resume`, `sd worktree restore` and requeue: it runs as an `exec` row
  too, audited the same way, but the runner executes it in its own
  process against the row it names, taking the repository alone as a
  serial row does and making no worktree, because the worktree it acts
  on is the very one a fresh worktree would be refused for, from B's
  round thirty. And `control`, for kill and for clearing a quarantine,
  from B's round thirty-one: those must act while an author runs or
  while survivors hold the repository, which is exactly when exclusive
  access is refused, so a control entry waits for nothing, and it does
  not wait for the runner either, from B's round thirty-eight: the state
  it exists to end includes a runner that hung or died with a child
  alive, so the dashboard's own process acts, which is the small
  authenticated supervisor outside the runner, behind the same identity,
  origin and token checks as every execution. The request writes the
  `exec` note first, as every execution does, then the server acts at
  once, in its own process, from the row alone: kill ends the named
  row's process group after the start-time check, writes the row `ending`
  with `killed by operator` as the outcome recorded, and leaves the rest to
  the runner's ordinary end run, which writes the terminal `blocked` with
  that note on its next tick or start; a row written terminal by the kill
  would be one the runner's start never reconciles, its lease held and its
  dirty work archived by nothing, which is item D's `archive/2026-09/2026-09-05-the-runner-works-the-queue/prd.md:766-773` and its
  criterion 4; clear kills the survivors the quarantine
  names, the same check first, and writes the quarantine lifted, which
  the runner reads on its next tick as it would after they exited on
  their own. A third control entry, `runner restart`, runs `launchctl
  kickstart -k` on the runner's label through the backbone's wrapper,
  requirement 9, and Today offers it beside a stale heartbeat. A control
  entry touches no worktree and no ref, which is why it needs no
  exclusivity; removal keeps it. Recovery from the iPad is therefore
  possible in every state the runner can be in, stopped included, and
  criterion 12 proves it with the runner stopped.
- Every execution is a `note` of kind `exec` on the item it ran for, or on
  the standing `system` item when it ran from System: who, what, when, exit
  code, and where the output is. The note is written before the command
  starts, with who, what, when and the output path, and completed after,
  with ended and the exit code; a server that dies mid-command leaves the
  note without an exit code, and the log shows that row as interrupted. A
  command whose note cannot be written does not run. The command log under
  System is where the operator reviews them, below.

It has to be a screen the operator wants to open. The current one has zero
GET requests; a plain table of rows will get the same. So:

- One stylesheet, hand-written, on CSS custom properties. Two palettes, light
  and dark, selected by `prefers-color-scheme` and nothing else: no toggle, no
  stored preference, the system decides. System font stack, generous spacing,
  cards for items and tiles for numbers, colour only where it carries meaning
  (status, blocked, cost over last week).
- Every write updates in place. Change a status, assign an item, promote a
  skill: the row or card changes where it is, with a short confirmation, and
  the page never reloads. One small vendored JavaScript file, no build step,
  no framework, no bundler; this repository has no build convention and gets
  none.
- Every chart, the timeline, the age histogram, the burn line, the two
  gauges and the scorecard, is SVG the server renders from the rows, by
  the operator's decision on 2026-09-05: no chart library, nothing drawn
  on the client, and the vendored script swaps a fresh SVG into place as
  it does any other fragment. A chart that is markup is read by a test
  without a browser, prints, and needs no second script under the CSP
  above; a tap on a bar is a link the SVG carries, not a handler.
- A command palette on one keystroke lists every action on the current screen
  with the command that does the same thing. This is the answer to forgetting
  commands: type what you want, see the command, run it or copy it.
- The palette runs command lines too, from the iPad, with the guards below.
- Touch first, keyboard second. Targets at least 44 points, no hover-only
  affordances, swipe where a list has one obvious action. The palette opens
  from a button as well as a keystroke. Layout holds at iPad widths in both
  orientations and on a Mac window. It installs to the iPad home screen as a
  standalone web app, with a manifest and safe-area insets.
- Keyboard where it is cheap: `j`/`k` through rows, `enter` to open, `/` to
  filter, `?` to show the map.
- Every list has the same three controls, rendered by one component. A
  filter field that narrows on any visible column and on the fields the
  section names; `/` focuses it and the URL carries it, so a filtered view
  is a link. A selection, one tap per row or select-all-shown, with the bulk
  actions that apply to the section: Backlog and Today, status change,
  priority, assign, run sequential and run parallel; Item notes, resolve;
  Skills, promote, demote and review; Providers,
  enable and disable; Usage and the command log, none, they are read-only. A
  bulk action writes one row per selected row and reports the count.
  `Run sequential` and `Run parallel` show a number before they act, asked
  for on 2026-09-05, and the number says what it is, from B's round
  thirty-one. With no budget typed it is an estimate and labelled one:
  per item, the bound of one author call at the author's price plus the
  bound of one review call at the reviewer's, summed, shown against every
  capped bill's remaining room and every `plan` bill's meter; an
  assignment makes as many calls as it needs and its reviews and
  integration review are calls too, so the estimate bounds nothing and the
  dialog says so in the same line. With `budget_usd` typed on the dialog,
  one number for every row in the batch, it is a bound, and a bound only
  where the library makes the calls, from B's round thirty-two: an entry
  with `url` is called through the library's one client, so every
  request reserves first and the budget holds; an entry with `start`,
  `claude -p` or `codex exec`, makes its own requests inside its own
  process, the library reads the usage after, and no reservation stands
  between the session and the vendor. So a budgeted batch whose author
  resolves to a `start` entry is refused at the dialog naming the entry,
  with the estimate offered instead, and a budgeted batch on a `url`
  author has item D's runner write `budget_usd` on each row beside
  `budget_minutes`, and the library's reservation, requirement 6, counts
  every call the assignment makes, nested reviews and the merge row's
  integration review included, against the row's remaining budget as
  well as the bill's room, so the sum of budgets is what the batch can
  spend and no more. Either way the dialog refuses when its number passes
  a cap, naming the bill. Pages
  of fifty rows with the total shown, once a list passes fifty; a filter
  applies before paging.
- Empty states say what to do next, with the button to do it. A backlog with
  nothing due says so and offers the filing form. Today with nothing open says
  so and shows the week's numbers.
- Nothing decorative that moves. Feedback animates; chrome does not.

### Requirement 6 — cost and the four numbers

Cost rows are told apart by `source`. One function in the library, `charge`,
writes every `run` row, one per provider call, keyed by the call id the
library issued when the call started. The runner, `sd-review` and the API
client all pass through it with the tokens the provider's reader reports;
none of them writes a cost row of its own. The row carries the `assignment`
and the review `pass` it ran under as context, so a call made by a review
inside an assignment is one row that both totals sum; the totals are sums,
never rows. A `meter` row is what
`local-agent-meter` samples every four hours, provider-wide plan usage with
no assignment, and it keeps writing the blog piece's data file until item C
decides otherwise. Per-item accounting is one query, shared by every
surface, that sums `run` and `bound` rows, from B's round thirty-eight:
a `bound` row is money the provider may have billed for a response that
was lost or a caller that died, so an assignment whose only charge was
lost still shows its cost, labelled estimated until the operator
reconciles it against the invoice on the Usage screen, and the four
numbers, the item screen and `sd usage` cannot disagree. `meter` rows
feed the plan-usage line on the Usage screen and nothing else, because a
provider-wide sample cannot be split between two assignments after the fact.
GitHub spend joins when a billing source is reachable; see the open question.

Shipping is an event, not a closure. `shipped_at` is set once, by the thing
that delivered: `sd-ship` when it merges a work item, the send-box action
when a `ready_to_send` item goes out, the import for a piece already
published. A declined idea, a closed report, a finished task, a demoted
skill reach `done` and are never shipped. Four numbers, computed weekly by
the library over items with `shipped_at` in the week, shown on Today:

- cost per shipped item: `run` and `bound` cost rows summed by `assignment`,
  joined to the shipped items; `meter` rows excluded;
- framework commits per shipped item: commits in the pack, this repository and
  the writing repository that week, divided by items shipped;
- assignment to merge: median hours from `assignment.started` to the item's
  `shipped_at`;
- reverts: commits whose message starts with `Revert` across the repositories
  in `repo`.

One cap exists from day one, on the capped provider bill. The `baseten` bill
row, seeded from the registry's `cap_usd_month: 50`, carries it, and the
dashboard raises or lowers it in one action. A cap on completed cost is an
alert, not a cap: cost arrives after the call, and calls from two
repositories or a direct review can each see room and together pass it. So
the library reserves before it dispatches. For each call on a capped bill it
computes a bound, prompt tokens plus the entry's `max_tokens`, times the
entry's `price:`, and in one SQLite transaction inserts a `cost` row of
`source: reserved` with the call id, the bound, the bill, the month and the
owner's pid, if the month's `run` and `bound` rows plus every `reserved`
and `sending` row still open on the bill, whichever month it was made in, plus this
bound stay within the cap; else it refuses. The open reservation counts
against whichever month asks, from B's round forty-two, because a
reservation made a minute before the month ends and dispatched a minute
after is money the new month owes and the old month's bucket cannot
hold it from the new month's callers; and when it settles, the `run` or
`bound` row carries the month of the claim, not of the reservation, and,
when the settlement falls in a later month than the claim, that month
too, counting against both months' caps until the operator settles it
to one against the invoice on the usage screen, from B's round
forty-eight: the claim and the wire are two steps, a caller can claim
before midnight and send after, and the vendor bills whichever month it
served the request in, which the ledger cannot know; a row charged to
the earlier month alone would release the later month's whole cap to
money the later month owes, so the ledger holds both until the invoice
says which, and the cost of that is one cap held twice for one call a
month, at most. A call made for an
assignment that carries `budget_usd` reserves against that too, on every
bill, capped or not, in the same transaction, and `budget_usd` is
accepted on a row only when every call the row can make goes through
this reservation, the author a `url` entry and the reviewers resolved
the same way, since a `start` entry's session calls the vendor itself
and a budget the library cannot stand in front of is a number, not a
bound: the row's exposure plus this bound must stay within the budget,
else the
call is refused and the runner ends the row `blocked` with a `budget
spent` note and the amount, so that a budget the operator typed bounds
the whole assignment and not its first call. Exposure is one sum
computed by one function for a bill's cap and a row's budget alike,
from B's round forty-seven: `run` and `bound` rows plus every `reserved`
and `sending` row still open, so that a call on the wire counts against
the budget as it counts against the cap, and two calls of one
assignment, a nested review beside its author's, cannot together pass
the budget by one being mid-flight. Reserved money is the
sum of `reserved` and `sending` rows, held nowhere else. Settling is one
transition of that row, by call id, from `sending`, and it happens once: to `run` with the actual cost after the
call, or to `bound` at the full bound when the response is lost, a timeout
or a dropped connection, because the provider may have finished and billed
it. A second settlement of the same call id is a no-op. A call id is one
attempt on the wire, from B's round thirty-six: the client makes no
automatic retry, its HTTP library's retries are set to zero and a test
asserts it, so a request the provider accepted while its response was
lost is billed once and settled to `bound` once, and a caller that tries
again does so as a new call with a new id and a fresh reservation,
refused when the room is gone; every attempt that can be billed has its
own bound, and the sum of bounds is the sum of attempts. A `reserved` row
whose owner pid is dead, or that is older than the entry's timeout, is
released, deleted from the ledger, by the next reservation on any bill
and by `sd usage`, in the one transaction a claim runs in, from B's
round forty-nine: the claim below is the only road to the wire and it
refuses a row that is not `reserved`, so a row the claim never moved
has provably spent nothing, and a process killed after reserving
neither holds the cap nor, as it did until round forty-eight, turns a
request never made into `bound` money the operator reconciles against
an invoice that never carried it; `bound` is a `sending` row's
settlement and no other's. A released reservation cannot be spent,
from B's round
forty-six: the client claims the reservation in one transaction just
before the request goes on the wire, `reserved` to `sending` with the
month of the attempt, and refuses to send when the row is no longer
`reserved`, because a caller that paused between reserving and sending,
past the timeout or past a month's end, would otherwise dispatch a charge
the ledger had already closed and given another caller the room for; it
reserves again as a new call and is refused when the room is gone. A
`sending` row is a wire attempt the ledger cannot yet cost, and the sweep
leaves it charged at its bound against the room, in the month of its
attempt, until the response settles it to `run` or its loss settles it to
`bound`; a `sending` row whose owner pid is dead settles to `bound` at
once, by the next reservation or by `sd usage`, and one whose owner lives
is the owner's to settle, however long, so an attempt that may have been
billed is never counted as nothing. The usage screen lists `bound` rows so the operator can
correct one against the provider's invoice. The permitted overshoot is
therefore zero on the sum of bounds, and the actual cost is at most the
bound, because every call on a capped bill is one the library makes: a
capped bill takes `url` entries only, and the registry reader refuses a
`start` entry whose bill carries a cap, naming the entry and the bill,
from B's round thirty-four. Round thirty-three had such an entry declare
a session bound, reserved at start and settled from the usage read, and
that is a guess a retry or one more model turn passes with the money
already spent; a cap the library cannot stand in front of is an alert,
and this item does not call an alert a cap. The operator points the
entry at the vendor's endpoint as a `url` entry or takes the cap off the
bill. **A `start` session is charged from its own exit**, from B's round
forty-two, which is what "the usage read" means and what it had never said:
the thing that started the session reads the total the session itself
reports when it ends and writes one `run` row, the assignment and the pass
on it. One row and not one per call, because the only reason to itemise is
a cap to stand in front of, and a `start` entry cannot sit on a capped bill
— the registry reader refuses it two sentences above. Every number
downstream reads a sum: cost per shipped item, the four weekly numbers, and
the usage report's grouping by vendor, provider, bill and role. The writer
is item D's runner, which is what execs `claude -p`, `codex exec` or the
entry's `start` line and the only thing that knows an assignment and a pass
stand behind the session. A session nothing started — the operator's own,
on the same plan — gets no such row and is not meant to: that spend is what
a `meter` row is, provider-wide with no assignment, on the sampler's
schedule. Three other readings were weighed and each fails the attribution:
the four-hourly sampler and a vendor billing endpoint are both
provider-wide, and `sd usage` may never be run at all. Round thirty-four said the two Baseten entries were `url` entries
already; they were not, item A's registry had the `prism` and `gito`
CLIs as `start` lines on that bill, and A's round twenty-two replaces
them with one `url` entry, `baseten`, through the library's client, so
the rule bites once, on day one. At the cap, provider fallthrough skips every entry on that
bill, a direct pick refuses by name with the month's total and the button
that raises the cap, and Today shows spent, reserved and cap. Cost rows for
API entries are tokens times the `price:` on the entry, so no billing API is
needed. No other bill has a cap; those numbers are read for a month before
any is set.

### Requirement 7 — nothing lives only in context

The library exposes note writes by kind so that a session records followups,
decisions, proposals and questions as it produces them, and one resolve
operation, `sd note resolve <id>`, which sets `resolved_at` and nothing else;
the dashboard's item screen has the same button on every open note. A note is
open while `resolved_at` is null. The `SessionStart` hook item A owns calls
the library for a bounded brief and injects it, alongside the handoff packet
if one exists and the `claude-mem` context it injects today. The brief is
the open `followup` and `question` notes of the item whose branch is checked
out, or of every not-`done` item in the repository when no branch matches,
newest first, cut at eight kilobytes with the count of what was cut and the
command that lists the rest, `sd note list <item>`. Decisions, proposals,
comments and executions never inject; they are read on the item screen or by
that command. Resolved notes stay in the item's history and never return to
a brief or to Today. A session resolves the followups it finishes as it
finishes them. The test is a session killed
mid-task: every followup it named is on its item, and the next session
starts from the open ones only.

### Requirement 8 — herdr brings the agents back

`local-herdr/` wraps `herdr` with one named persistent session. On start it
resumes each pane's last agent session: `claude --resume <id>` for Claude Code,
the equivalent for Codex, read from a small state file the wrapper maintains as
panes start. If `herdr`'s session model cannot carry this, the wrapper is
dropped and a request goes to `herdrdev/herdr` with the state file's shape as
the proposal.

### Requirement 9 — the backbone knows the new pieces

This repository already keeps a machine honest: `local-machine-setup`
provisions from a profile and reports drift, `local-cron-jobs` installs
launchd jobs and a watchdog catches the silent ones, `local-health-check`
reads every agent's state nightly, `local-maintenance` rotates logs and
watches disk, certificates and backup freshness, `offsite-verify` restores the
NAS snapshot (amended 2026-10-03, sd:1157, see the end). The pieces this item adds, the database, the runner, the
dashboard's HTTPS route, the worktrees, are new to all of them, and a
backbone that does not know a piece does not protect it. Six things, asked
for on 2026-09-05, three of them here and three with the runner, item D.

- **The profile builds it.** `personal.agent` gains `local.system-tools.sd-runner`
  beside `local.system-tools.sd-dashboard`; the profile's setup stages gain
  `local-sd-db/sd-db.sh init` and the `tailscale serve` route to the
  dashboard's port; `machine-setup.sh setup personal --apply` on a fresh
  machine leaves the database initialised, both agents loaded, and the
  route present, and `machine-setup.sh status` reports any of the four
  missing as drift. `machine-setup.sh doctor` gains four checks: the
  database opens and passes `PRAGMA integrity_check`; the runner is loaded
  and its heartbeat is fresh; the dashboard answers over its HTTPS name;
  the serve route exists.
- **The runner's pulse, sleep and keys** are specified with the runner,
  item D, requirement 3: the heartbeat row `doctor` and Today read, the
  `caffeinate` under each session, and the environment a session
  receives.
- **Rows and worktrees age.** One retention table, read by one nightly
  prune that runs inside `sd-db-backup` after the backup has passed, never
  before: `cost` rows never, from B's round forty, since requirement 6
  admits a call by summing the assignment's rows and the item screen
  reports an item by them, and a blocked assignment resumed or an item
  shipped after a prune would spend or report against a ledger with a
  hole in it, while a year of calls is thousands of small rows and no
  weight; `exec` output files ninety days
  and the `exec` note never, since the note is the audit record,
  identity, entry, arguments, exit code, and leaves only with its item,
  requirement 5, from B's round thirty-seven; backups thirty files,
  `heartbeat` rows one -- and no request log, since none is written. A kept
  worktree is never removed by the prune, since it may hold work; after
  fourteen days Today counts it as stale beside the `discard` and `resume`
  it already offers. The prune writes one `report` row with the counts
  it removed, and refuses to run when the night's backup did not pass.
- **The backup is in the backup.** Requirement 1's dated directories,
  the database with `providers.yaml` and `commands.yaml` beside it, live under
  `~/Documents/sd-backups/`, inside a pair `backup-verify.conf` already
  has, so the clone copies them and the monthly verify samples them with
  no new configuration, and nothing under `~/.local/share/sd/` is relied
  on for recovery: the live database is restored from a dated file, a
  clean worktree is disposable by construction, and a kept one is in its
  bundle under the same backed-up path, item D, so that a restored
  database never names work that was lost with the disk.

### Requirement 10 — the fixture harness lands first

Dozens of criteria in this item, item A and item D say a fixture
repository, a recording GitHub fixture, a provider whose `start` is a
script, stubbed `launchctl`, `tailscale` and `curl`, and nothing named the
thing they assume. It is one package, `sd_db.testing`, shipped with the
library so that the pack's tests and this repository's import the same
one, and it is the first slice to land, before any criterion that uses it
is claimed, asked for on 2026-09-05. It holds: a fixture remote, a bare
repository with a default branch, a protection state and a pull-request
table, behind an HTTP double of the GitHub calls the pack and the runner
make, open, state, `merge_commit_sha`, mergeable, merge with `sha`, checks,
protection, collaborators, that records every call and answers from its
table; a recording double of each provider API the registry names,
answering from canned replies; a fixture provider whose `start` is a
script the test writes; stubs for `launchctl`, `tailscale`, `curl`,
`caffeinate`, `lsof` and `local-notify` that answer from a table and
record; and a fixture home, `HOME` pointed at a temporary directory with
`~/.local/share/sd/`, `~/Documents/sd-backups/` and
`~/.config/shell/env.sh` inside it. A criterion that names one of those
uses this harness and no other double, so that a test that passes proves
the same thing in both repositories, and a change in what GitHub answers
is one change.

## Landing order

Items A, B and D depend on each other in both directions, so they land in
slices, each its own pull request, in this order, by the operator's
decision on 2026-09-05. A slice claims only the criteria its text names.

1. B: the fixture harness, requirement 10, then the database, the library
   and the migrations as rehearsals, requirements 1 to 3: import and
   verify, retire nothing, every source still authoritative and still
   written by what writes it today.
2. A: the registry reader, `sd-ship`'s tiered path, the protection, the
   trailers on the library. `docs/work` and the
   register retire here, in the run that lands their writers, requirement
   2's one sitting.
3. B: the dashboard read-only, the five sections without a write control,
   and the reports on rows; `dependabot-daily` writes `dep` rows and keeps
   its own merger. `index.sqlite` retires here, after the sync
   requirement 2 names has brought in a new issue; the vault's ideas
   retire under item C, and TaskNotes on the operator's word.
4. B: the dashboard's writes, the palette, cost and the four numbers, and
   the backbone, requirements 5, 6, 7, 8 and 9. An assignment created here
   waits `queued` until the fifth slice, and the library's `cancel`
   takes it back without a runner, requirement 1.
5. D: the runner, after a spike of a few hundred lines against the harness
   that runs one assignment end to end, whose findings go on D before its
   prose is reviewed again; then, once the runner's merge check passes,
   the one commit that cuts `dependabot-daily` over to enqueue-only.

A slice that needs a later slice's piece stubs it and says so on Today: an
assignment row before the runner is shown as waiting on item D.

## Acceptance criteria

1. `~/.local/share/sd/sd.db` exists after `local-sd-db/sd-db.sh init` and
   holds the tables `sd_db.schema.TABLES` names and no other: the eleven day-one tables above, plus the five that migrations 004, 005, 006 and 011 added, sixteen at schema version 11 (restated 2026-09-23; this said "the eleven tables named above"). A record
   kind with no home is added to that tuple, by migration, before it is written;
   a grep of the library for an insert into any table not in the tuple
   returns nothing. A test runs `sd-db.sh migrate` twice and asserts it
   is idempotent, that opening the database alone applies no migration,
   that a library built for an older version refuses to open a migrated
   database naming both versions, that one built for a newer version
   refuses to write naming the command, and that `sd_db` imported from
   the pack's virtualenv resolves to a file outside this checkout.
   `sd-db-backup` is loaded,
   `~/Documents/sd-backups/` holds a dated directory after its first run
   with the database, `providers.yaml` and `commands.yaml` in it, and
   that run's restore passed `PRAGMA integrity_check` and found its
   checkpoint row; a test restores a dated directory into an empty fixture
   home and asserts the runner resolves a provider and the palette runs a
   fixture entry from it once the restore is resumed. A test takes a
   snapshot while an `exec` row is `queued` and a capped bill has a
   reservation, lets the command run and the cost settle, restores the
   snapshot, and asserts the runner dispatches nothing, the `exec` row is
   `blocked` with a `restored` note and its command did not run again,
   the bill refuses a call until its spend is entered, Today shows the
   restore first, and after `sd restore resume` with the spend entered a
   new row dispatches and the old `exec` row is still blocked. A test
   gives an assignment on an uncapped `url` author a `budget_usd` of one
   call's bound, snapshots before the call, lets it spend to `blocked`
   with `budget spent`, restores and resumes, and asserts the row
   carries `budget restored`, a requeue without a new budget is refused
   naming the restore, and a requeue with one typed reserves the first
   call against the new number alone. Tests run
   the backup against an empty fixture database,
   one whose newest row is a month old, and one with a fresh row, and
   assert all three restores pass; a test truncates the copy and asserts
   the count comparison fails. A test makes the database unopenable, runs
   the backup job, and asserts one email leaves through the cron mail path
   and nothing is written to the database; a second test fills the backups
   volume and asserts the same. `backup-verify.conf` lists the backups
   directory.
2. `sd_db` is importable from the pack's virtualenv, and a grep of the pack,
   this repository and the dashboard for `sqlite3.connect` returns only the
   library. Every write in the codebase goes through a named library function.
3. `providers.yaml` resolves `author` and `reviewer` to different providers; a
   test asserts the library refuses a registry where they match, and that
   adding an `exo` entry with a URL resolves without a code change.
4. `sd today` and the Today screen render from the same library query; a test
   asserts they list the same item ids in the same order.
5. Each migration in requirement 2 runs twice and reports the same counts the
   second time with zero new rows. After the `index.sqlite` migration,
   `sd-status` reports the same shadow counts it reported before, and
   `index.sqlite` is gone; the retirement step refuses until the writers to
   the source are stopped, the frozen source and the rows agree by identity
   and content, and a snapshot taken after the import has restored with
   those rows; asserted by tests for each refusal: a writer still loaded, a
   source row changed after the import, no snapshot, and a snapshot that
   predates the import. Each migration in requirement 2 has the same gate.
   The idea import keeps every ladder word in `stage`, asserted by comparing
   the set of stages in the rows with the set in the frozen source; a
   fixture note with a stage the table does not name makes the import refuse
   with the word, and the mapping table names every transition target in
   the writing manifest, asserted by reading the manifest.
6. Every `docs/work/*/prd.md` in a repository the `repo` table holds has an
   `item` row, asserted by enumerating those repositories from the table and
   joining, and the count of rows with no file is zero. A test clones a
   registered repository under the worktrees directory and asserts the
   enumeration does not widen and the criterion still passes, since a
   runner's clone is not a registered repository.
7. A status change writes the row and touches no file, asserted by a
   test that changes status and hashes the item's files; `sd-docs-lint`
   fails on a `status:` line in any `prd.md` under `docs/work/` outside
   the archive, asserted with one seeded, and its rule 2 keeps working
   without that line: `check_ready` reads the status from the row where the
   repository's `status_source` is `row` and from the line where it is
   `file`, asserted with one `in_progress` fixture that fails all three of
   rule 2's checks — acceptance criteria stated, no open `BLOCKING:` line,
   branch recorded — in both modes. Rule 2 gates on
   `if status not in WORKABLE_STATUSES: continue` today, so the retire
   commit would switch it off for every active item in every registered
   repository while every clause of this criterion still passed; run on
   2026-09-05 the fixture fails three checks with the line and reports
   `rule 2 failures: []` without it, from B's round fifty-five; and the
   `docs/work` migration's
   retire step removes every item's line outside the archive in one
   commit, refuses under a pack whose `sd_lib` has no `delivered`, and
   the import before it lands a `done` item as a `done` row with its
   line untouched; a line changed by the old command between the import
   and the sitting is read by A's reader over the row and carried into
   the row by the sitting's final import, which sets the repository's
   `status_source` to `row` and adds the tracked marker
   `.status-source` under the retiring repository's `docs/work/` that A's reader without a database reads,
   from A's round forty-one, after which a line in a retained worktree
   is ignored and the old command refuses, asserted by a diff of the
   fixture repository after each step and the row, and the sitting killed
   after the import and before `row`, and after `row` and before the
   commit, reruns to the same end, from C's round five; a test completes
   the sitting on the fixture repository, restores the snapshot it
   verified against into the retired checkout, and asserts the restore
   sets `row`, reimports no line, and a read and a write succeed,
   restores the cutover snapshot and asserts nothing reconciled, from
   B's round fifty, and restores a snapshot taken before the final
   import into the retired checkout and asserts the row stays
   `retiring` with the note, a write refuses, and `sd restore reimport`
   lands rows equal to the lines at the marker commit's parent, or at
   the commit the marker's line names where it names one, from C's
   round thirty-two, and
   sets `row`, from B's round fifty-one, and a `prd.md` edited and not
   committed makes the sitting refuse naming it, from C's round eight;
   a fixture with an item on a branch alone and another whose branch
   carries a newer line than the default has the sitting land the
   branch-only item from its branch with `source_commit` set and refuse
   naming the divergent item and both branches until the lines agree,
   and a restore of a pre-import snapshot followed by `sd restore
   reimport` recovers the branch-only item from its `source_commit`,
   from B's round fifty-four;
   a test starts a
   status change from a second checkout of the fixture repository while
   the sitting stands between its freeze and its switch and asserts the
   write is refused naming the sitting, no line and no row changed,
   that the same change lands in the row after `row`, and that with a
   verify difference seeded the source returns to `file` and the
   refused change lands in the line, from B's round forty-nine. A test changes an item's status three times and
   asserts three `status_change` notes in order, each with old, new and
   time, that the item screen lists them, and that a status write outside
   the library's one function is not possible, by grepping the repository
   for a second `UPDATE item SET status`. Transitions: a test asks `done`
   for an unmerged work item from the
   item screen, `sd task status` and the palette's status entry, and asserts
   each is refused with the library's one sentence, "work completion requires verified delivery or cancellation evidence" (amended 2026-09-17, approved by the owner 2026-09-17 (note 2706): the clause said "naming the merge and offering cancel"; the refusal `source:local-sd-db/sd_db/workflow.py::change_status` raises names delivery, which for a work item is the merge confirmed through the remote, clause 7.21, and cancellation; the intent, that a work item closes on merge or on cancel and on nothing else, stands; the clause also asked the board's drag, a bulk action and `sd status`: the board is read-only cards with no drag handler and no `BulkAction` is constructed, measured in `source:local-project-dashboard/tests/test_criterion_7.py::Criterion7`, so those two carry no status write and are dropped, and the verb is `sd task status`), the row is
   unchanged and no `status_change` note was written (7.17 names decided 2026-09-16, owner decision (a), the clause's fixture test still owed: the `sd status` surface is the pack's existing `sd task status <item> <status>`, registered in `bin/sd_work.py` over `source:local-sd-db/sd_db/workflow.py::change_status`; no literal `status` group is built; system half landed on `feat/sd-234-criterion-7-fixture-tests`, `local-sd-db/tests/test_criterion_7.py` and `local-project-dashboard/tests/test_criterion_7.py`; pack half owed); cancel from the
   item screen writes `done` with a `cancelled` note and, on an item
   whose triad reached the default branch, the next merge `sd-ship`
   makes in that repository carries `Closes:` for it in its message and
   changes nothing for it in the tree, asserted in the fixture checkout
   with no `Delivers:` in the history and no pull request beyond that
   merge's; a status write on
   an item whose assignment is `running` is refused naming the row from
   every surface above, the refusal naming `cancel` for a `queued` row
   and the control entry for a `running` one (7.19 names decided 2026-09-16, owner decision (a), the clause's fixture test still owed: the assignment is created by the pack's existing `sd run --sequential|--parallel`, registered in `bin/sd_runner.py` over `source:local-sd-db/sd_db/runner.py::enqueue`, the `sd status` surface is `sd task status <item> <status>`, and the `cancel` the refusal names is `sd assignments cancel` / `sd runner cancel`; system half landed on `feat/sd-234-criterion-7-fixture-tests`, `local-sd-db/tests/test_criterion_7.py` and `local-project-dashboard/tests/test_criterion_7.py`; pack half owed. Gap recorded 2026-09-17, decided by the owner the same day (note 2706; delivered by sd:991, `sd runner cancel` ends the row `cancelled` with the note): a `running` row with no `runner_run`, one written by hand or from before the runner recorded attempts, has no recovery on any surface. `source:local-sd-db/sd_db/runner.py::request_cancel` refuses it, "only queued work or a running owned attempt can be cancelled"; `source:local-sd-db/sd_db/runner_controls.py::control` refuses it, "this assignment has no owned runner attempt to control"; `source:local-sd-db/sd_db/operations.py::cancel_assignment` refuses every `running` row, "running assignment has no supported cancellation backend"; the item screen renders no cancel and says "There is no supported cancellation backend for this legacy assignment; no owned runner attempt was recorded."; and the status write is refused with "item N has queued or running assignment M; it is a running assignment without a runner run, and there is no supported cancel for it yet (sd:234)". The item stays unwritable until the row is edited by hand or by a direct library write, which `update_assignment` does not guard from `running`. The recovery is an owner decision); with no runner process in
   the fixture at all, a test assigns an item, asserts the row is
   `queued` and a status write is refused, cancels it from the item
   screen and from `sd assignments cancel` / `sd runner cancel`, and asserts the row is `cancelled`
   with `cancelled by <who>`, the caller's value (`dashboard` from the item screen, the login from the pack's verbs, `operator` in the fixtures), on the row's `result` from the runner cancel and in an item note from `cancel_assignment`, the item takes a status write again,
   and nothing waited on a heartbeat (7.20 names decided 2026-09-16, owner decision (a), the clause's fixture test still owed: the `sd assign cancel` surface is the pack's existing `sd assignments cancel <id>`, registered in `bin/sd_operations.py` over `source:local-sd-db/sd_db/operations.py::cancel_assignment`, or `sd runner cancel <id>`, registered in `bin/sd_runner.py` over `source:local-sd-db/sd_db/runner_controls.py::control`; no literal `assign` group is built; system half landed on `feat/sd-234-criterion-7-fixture-tests`, `local-sd-db/tests/test_criterion_7.py` and `local-project-dashboard/tests/test_criterion_7.py`; pack half owed; amended 2026-09-17, approved by the owner 2026-09-17 (note 2706): the clause said `blocked` with `cancelled by operator`; both cancels end the row `cancelled`, the library's terminal status guarded in `source:local-sd-db/sd_db/writes.py::update_assignment` and accepted by `requeue`, and the note reads `cancelled by <who>` with the caller's value, so the page now follows the library; `source:local-sd-db/tests/test_criterion_7.py::Criterion7` asserts `cancelled` and `source:local-project-dashboard/tests/test_criterion_7.py::Criterion7` asserts `cancelled by dashboard`); and a merge confirmed through the
   fixture remote writes `done` with `shipped_at` and one
   `status_change` note.
8. The four vault process jobs — `obsidian-review-daily`, `blog-idea-accept`,
   `tips-accept`, `tips-weekly` — are absent from `local-cron-jobs/jobs/` and
   from `~/Library/LaunchAgents`, asserted by enumeration, not by list.
   Moved to item C on 2026-09-12 with requirement 3's amendment: it is
   asserted there, in the change that removes the vault's views, routine
   and queues, and not by this item. `vault-cleanup` and `vault-map` stay
   and are asserted nowhere.
9. Moved to item D as its criterion 1 on 2026-09-05.
10. Moved to item D as its criterion 2 on 2026-09-05.
11. Moved to item D as its criterion 3 on 2026-09-05.
12. The five dashboard sections exist and each reads through the library.
    Every button has a visible command beside it; a test walks the rendered
    pages and asserts no write control lacks one.
    The stylesheet defines both palettes as custom properties, the dark one
    under a single `prefers-color-scheme: dark` media query, and no page
    stores or offers a theme toggle; a test parses the stylesheet and asserts
    both palettes name the same set of properties. Every write control updates
    in place: a test performs each write through the page and asserts no full
    navigation followed. The command palette lists every action on the screen
    with its command, asserted against the same enumeration as the button
    test. No build step: `local-project-dashboard/` contains no
    `package.json`, `node_modules`, or bundler config, and the dashboard
    serves one CSS file and one JavaScript file from disk. The server binds
    loopback on one port and nothing else, asserted by a test that reads its
    listeners, and
    refuses to start when `tailscale serve status` shows no HTTPS route to
    that port, asserted with a stubbed `tailscale`; a request whose
    `Tailscale-User-Login` is not the operator's gets a refusal, a request
    carrying that header from a non-loopback peer gets one, and a write
    without a same-origin `Sec-Fetch-Site` gets one too, each asserted with
    stubbed headers. One write from the iPad over the HTTPS name is
    performed by hand and recorded on this item with the date, because a
    stubbed header cannot show that the browser sent it.
    Content: a fixture artifact carrying a `script` tag, an image with an
    `onerror` handler, a `javascript:` link, a `data:` link and a raw HTML
    block is rendered on the item screen, and a fixture command's output
    carrying the same is rendered in the command log; a browser test
    asserts that the global the payload would set is absent, that the
    console shows the policy's refusal, that no palette request left the
    page, and that every response, error pages and the JavaScript file
    included, carries the policy header with `frame-ancestors 'none'`
    and `X-Frame-Options: DENY`; a browser test serves a page on a second
    origin that frames an authenticated dashboard screen and asserts
    the frame renders nothing of it, the console shows the refusal, and
    a click at the framed control's position leaves no request; a grep
    of the templates for `|safe`, `innerHTML` and `mark_safe` returns
    nothing.
    Execution: `commands.yaml` exists and every palette entry on every screen
    maps to one entry in it, asserted by enumerating both; a request naming
    an entry absent from the file is refused, a request carrying a command
    string is refused, a placeholder value that is not an existing item id or
    a registered provider is refused, a request without the session cookie
    or the execution header, with a header that is not the cookie's
    session's token, with a token rendered for another login, or with one
    from before the rotation is
    refused, and a grep of the dashboard for `sh -c`, `shell=True` and
    `os.system` returns nothing; each asserted by a test. A permitted
    execution writes an `exec` note before the command starts and completes
    it with the exit code after, asserted against a fixture command; a test
    kills the server mid-command and asserts the note exists without an exit
    code and renders as interrupted; a test makes the note write fail and
    asserts the command did not run. A test keeps a dirty worktree, taps
    `discard` on the item screen from the fixture browser, and asserts a
    `supervisor` `exec` row ran with no new worktree, the kept worktree is
    gone and the next row on the item starts; the same for `resume` after
    a commit in the kept worktree, and a `worktree` entry requested on
    the item while the worktree is kept is refused naming the path. A
    test taps `kill` on a running author from the fixture browser and
    asserts, before the response returns and with the runner process
    stopped, that the author's process group is gone, the row is
    `ending` with `killed by operator` recorded as the outcome, the
    `exec` note carries the tap, and no worktree was made, and that the
    row stays `ending` while the runner is stopped; the same test starts
    the runner and asserts its first tick writes the terminal `blocked`
    with that note, which is requirement 5's split and item D's
    criterion 4 in `2026-09-05-the-runner-works-the-queue/prd.md`: a row
    written terminal by the kill is one the runner's start never
    reconciles. A test quarantines a repository with
    a `setsid` survivor, stops the runner, taps `clear` on the item
    screen, and asserts the survivor is gone at once, and that after the
    runner is started the quarantine is lifted on its first tick and a
    row queued for the repository started; a test taps `runner restart`
    against a stale heartbeat and asserts the fixture `launchctl` saw
    `kickstart -k` on the label; the first two also while a parallel
    author in the same repository keeps running untouched. The
    command log renders every `exec` note with the
    fields requirement 5 names, and no page offers a control that deletes
    one; a test runs three fixture commands, one failing, and asserts all
    three appear with device, login, entry, values, exit code and output,
    and that `sd exec-log` lists the same three.
    Lists: a test seeds sixty rows into each list the section names, asserts
    the first page shows fifty with the total, that a filter on one column
    narrows before paging, that select-all-shown plus a bulk action writes
    exactly the shown rows, and that a read-only list offers no bulk control.
    Views: a test renders the Backlog fixture as list, board and matrix and
    asserts the same rows, the same filter result and the same selection
    carry across the toggle and the URL; a card dragged to `blocked` on the
    board writes the status change; the matrix places a high-priority item
    with no `due` in important-not-urgent and a `ready_to_send` item four
    days old in urgent; and `Run sequential` from a quadrant creates the
    chain. A test selects ten items whose estimates sum past a capped
    bill's room and asserts the run dialog shows the sum labelled as an
    estimate and refuses naming the bill, that a selection within room
    shows the estimate and creates the rows, that with `budget_usd` typed
    on a `url` author the dialog shows the sum of budgets labelled as a
    bound and every created row carries it, and that with `budget_usd`
    typed on a `start` author the dialog refuses naming the entry and
    offers the estimate. A test renders the runner board with one queued, one running and
    one blocked row and asserts each in its lane, the blocked row's reason
    and the queued row's `after` on their cards. Charts, each asserted
    from the SVG the server returns and not from a browser: a test seeds two
    repositories with four assignments, one waiting on another's
    repository and one past its budget, and asserts the timeline draws
    four bars in two lanes, the wait hatched with its reason, and the
    overrun marked; a test seeds open items at one, five and twenty days
    in status and one `ready_to_send` at four days, and asserts the
    histogram's buckets and series, and that tapping a bar filters the
    list to its items; a test seeds a month of `cost` rows against a capped
    bill and asserts the burn line's points, the cap rule, and that the
    projection crosses the cap on the day the seeded rate implies; a
    `plan` bill with a meter row at forty percent and ninety percent draws
    two gauges at those values; a test seeds three passes, one blocking
    finding and one fallthrough for a provider and asserts the scorecard's
    four numbers.
    Layout: the browser test renders every screen at an iPad viewport,
    portrait and landscape, and asserts no horizontal scroll, every control
    inside the viewport, and every touch target at least forty-four points
    on a side; at a phone viewport it asserts the same for Today, the
    Backlog list, the Item screen and the palette, and for the board, the
    matrix and the charts only that they render read-only or fall back to
    the list without an error. The home-screen install and one session on
    each device are checked by hand and recorded on this item. Whether the screen is one the
    operator wants to open is checked by hand: the operator uses it for a week and `state` holds
    `checkpoint` rows keyed `operator-action` on five of seven days, counted by the query under
    criterion 12 in `implement.md` and recorded on this item with the count (note 3730, 2026-09-23).
13. Status change, assign, promote, demote and `merge_policy` each round-trip
    from the dashboard to a row, and promote opens a pull request in the pack
    through the library; the dashboard code contains no `git` or `gh` call.
    `protect main` on a fixture repository the operator owns sets the four
    settings through the recording API and the row shows protected after;
    on a fixture repository they do not own the page offers no such action
    and the request is refused.
    Enable, disable, reorder and cap each round-trip to a `provider` or `bill`
    row, the registry file's hash is unchanged afterwards, and the next
    `sd-review` resolution in the pack reflects the change; a test raises the
    cap and asserts a bill that was skipped is resolved again.
14. Every report that reaches the operator by email today has a `report` item
    row after its job runs, and keeps its email until the operator asks for
    its retirement, which the pack accepts only once the adoption gate in
    criterion 12 is recorded on this item; a test runs each converted job
    before the ask and asserts one row and one email, asserts the ask is
    refused before the gate, and after the ask, a row and no email unless
    the row carries `attention`, in which case one email and, with
    `local-notify` stubbed, one push carrying the row's title, and a plain
    `report` row none; a test kills the job between the commit of an
    `attention` row and the send and asserts the row shows both
    deliveries pending, that the next sender, the dashboard's tick in
    the fixture, sends one email and one push and marks both with a
    time, that a further tick sends nothing, and that two senders
    started on the same pending row at once, one paused inside its
    send, deliver each channel once, the second finding the claim,
    and a sender killed inside its send leaves a claim the next tick
    retakes after the lease, from B's round fifty-three. The list of jobs is enumerated from `local-cron-jobs/jobs/` and
    every entry that emails is either converted or named as an exception on
    this item. The store's own failures are the other named exception:
    `sd-db-backup` and the report jobs keep the cron mail path for a failure
    to open, write or back up the database, asserted by the tests under
    criterion 1.
15. A `meter` row carries a window and a percentage, not money. The sampler's
    ledger holds neither dollars nor tokens: over 167 readings on 2026-09-05,
    `usd`, `costUsd` and `tokens` appear **zero** times, and what each reading
    holds per provider is `windowMinutes` with `usedPercent` for a primary and
    a secondary window. So `usd`, `tokens in` and `tokens out` are null on a
    `meter` row, which the four weekly numbers already assume by excluding
    `meter` rows, and one row is written **per provider per window** rather
    than per provider. Criterion 12's two gauges at forty and ninety percent
    read two rows, not two fields of one; an earlier draft required one row to
    carry two percentages, which the schema had no column for either way.
    `meter` cost rows accumulate per provider on the meter's schedule, one
    `run` cost row appears per provider call with its `assignment` and `pass`
    set, and cost per shipped item sums `run` and `bound` rows only; a test
    writes one `meter` row and two `run` rows against two assignments in the
    same four hours and asserts each item's cost is its own; a test drops
    the response to an assignment's only call and asserts the item's
    cost, the four numbers and `sd usage` all show the `bound` amount
    labelled estimated, from one query; a test runs a
    review pass inside an assignment through a fixture provider and asserts
    the call is charged once and appears in both the assignment's and the
    pass's total; a grep of the runner and `sd-review` for a cost insert
    returns nothing. The cap reserves: a test runs two calls concurrently
    against a bill with room for one bound and asserts exactly one
    dispatches; a test asserts a call whose bound alone exceeds the remaining
    room is refused; a test gives an assignment on a `url` author a
    `budget_usd` of one call's bound on an uncapped bill, makes the
    recording double answer twice, and asserts the second call is
    refused, the row is `blocked` with `budget spent`, and a nested review
    call inside the assignment counted against the same budget; a test
    holds an assignment's first call in `sending` at eight of a budget of
    ten and asserts a concurrent second reservation of eight against the
    same assignment is refused, and the same on a bill whose cap is ten,
    both through the one exposure function, asserted by a grep for a
    second sum over `cost` rows; a test
    asserts a `budget_usd` on a row whose author is a `start` entry is
    refused by the library naming the entry, and that a `start` session
    whose script calls the vendor double three times is charged one
    `run` row carrying the total the session reports at its exit, with no
    per-call `reserved` row and no per-call `run` row either, which is the
    stated reason, from B's round forty-two; a test asserts the registry reader refuses a
    `start` entry whose bill carries a cap, naming the entry and the
    bill, that raising a cap on a bill that has a `start` entry is
    refused the same way from the dashboard, and that a `start` session
    on an uncapped bill whose script spends past any number runs to its
    end and is charged one `run` row from the session read at its exit,
    the runner writing it; a test asserts a reservation is settled to the actual
    cost after the call and that settling it again changes nothing; a
    test reserves on a capped bill a minute before the month ends,
    advances the clock past midnight, and asserts a second reservation
    in the new month counts the open one against the new month's cap,
    that dispatching the first settles a `run` row carrying the new
    month, and that the old month's total does not include it; a test
    suspends the caller between reserving and sending, once past the
    entry's timeout and once across the month's end with `sd usage` run
    in between, and asserts in both that the reservation was released,
    the ledger holding no row for its call id and the room free, that
    the resumed caller's send is refused with no request
    on the wire, and that its new reservation is refused when another
    caller took the room; a test suspends the caller after the claim,
    with the row `sending`, runs `sd usage` and a reservation on the
    same bill, and asserts the row stays `sending` and counted, that the
    resumed caller's response settles it to `run`, and that the same
    row with its owner killed settles to `bound` on the next
    reservation; a test claims a minute before the month ends, suspends
    the caller across midnight, lets it send and settle, and asserts
    the `run` row carries both months, a reservation of the new month's
    full cap is refused by that amount, the old month's total still
    holds it, and the operator's correction on the usage screen to one
    month releases the other; a test
    kills the provider mid-call and asserts the row becomes `source: bound`
    and still counts against the cap; a test makes the recording double
    accept a request and drop the response, and asserts one `bound` row,
    no second request on the wire, and that the caller's retry is a new
    call id whose reservation is refused when the room left is one bound
    short; a test kills the calling process after
    it reserved and before it claimed and asserts the next reservation
    releases the orphan, no row for its call id and the room free,
    and kills another after the claim, the row `sending`, and asserts
    the next reservation settles that one to `bound` and the cap still
    counts it, from B's round forty-nine. Shipped is not done: a test closes
    a declined idea, a report and a merged work item in one week and asserts
    the four numbers count one shipped item, that `shipped_at` is set only
    on the merged one, and that a second merge of the same item does not
    move it. The four weekly numbers render on Today with their
    inputs visible on hover or in a detail view. The usage report groups cost
    rows by vendor, provider, bill
    and role for the week and the month, and `sd usage` prints the same
    numbers; a test seeds rows across three providers and asserts both
    surfaces agree.
16. Three followups written through the library during a session appear in
    the context the `SessionStart` hook injects in a new session in the same
    repository, without `sd-handoff` having been called. After one is
    resolved, the next session's context carries the other two, Today lists
    two, and the item screen shows all three with the resolved one marked;
    a test asserts each. The brief is bounded: a test seeds a `done` item
    with open notes and a live item with enough followups to pass eight
    kilobytes, and asserts the `done` item's notes are absent, the brief is
    cut at the limit with the count and the listing command, and no
    decision, proposal, comment or `exec` note is in it.
17. After `herdr` restarts, each pane resumes the agent session it held,
    asserted by hand once and recorded on this item with the `herdr` version;
    or the wrapper is absent and the upstream request is linked.
18. `docs/work/2026-09-05-one-database-one-front-door/` is the first work item
    in this repository, and this repository's guide names `docs/work/` and the
    pack's lint as the place and the check.
19. Review on a skill in each of the three states opens a `skill-review`
    item and writes at least one `proposal` note through a fixture reviewer;
    applying two accepted proposals creates one assignment whose brief holds
    both and nothing else, and the runner opens one pull request in a fixture
    pack that changes only the named files; `sd skill review <name>` writes
    the same item. A skill whose last change was authored by the reviewer's
    vendor is reviewed by the next entry, asserted with a two-entry
    registry.
20. Moved to item D as its criterion 4 on 2026-09-05.
21. Dependencies. A test runs `dependabot-daily` against a recording GitHub
    fixture with four pull requests across two repositories, one patch,
    one grouped with a major inside, one Docker digest and one in a
    repository with no CI, and asserts one `dep` row per pull request with
    the class and reason `ROUTINE.md` gives them; before the cutover,
    that the job merged the patch itself, its row is `merged` with
    `shipped_at`, no `merge` row exists, and a merge from the selection
    is refused naming the slice; after the cutover, that only the patch got a
    `merge` row, that the runner merged it and its row is `merged` with
    `shipped_at`, and that the job's own merger is gone from its script,
    asserted by grep; and in both, that the run's
    `report` row names a third repository skipped by the owner gate, and
    that no `dep` row exists for it. A test selects three held rows in two
    repositories, a major, one in a repository with no CI and a digest,
    and merges them, and asserts each `merge` row carries `operator`,
    that the fixture saw the merges one at a time per repository in
    ascending number with the class conditions skipped and the
    mechanical ones checked, a rebase comment on the row the merge made
    stale, and the count reported; a `routine` row written by hand for
    the major is refused by the runner naming the condition, and an
    `operator` row whose pull request has a failing check is refused
    naming the check. A test ignores a
    major and asserts the `@dependabot ignore this major version` comment
    and the stage `ignored`. A test assigns a held row and asserts a `work`
    item with one `author` assignment whose brief names the pull request. A
    test advances the clock past the job's schedule with no run row and
    asserts Today shows a missed run. A test overlaps a bulk merge of two
    held rows, the morning job's enqueue, and an `auto` item's `merge` row
    in one fixture repository and asserts the fixture remote saw the merges
    one at a time, none while another was between rebase and merge, and
    each preceded by its own condition check. A test moves a pull
    request's head after the runner validated it and before it merged, by
    a fixture rebase, and asserts the merge call named the validated head,
    was refused, and that the row re-validated the new head before merging
    it; the same asserted for an item's `merge` row under item D's criterion 2. `sd
    deps` lists the same rows as the page, asserted after the first test.
22. Backbone. `personal.agent` names `local.system-tools.sd-runner`, asserted by a
    test that reads the profile; `machine-setup.sh setup personal` in dry
    run against a fixture home with none of the four pieces lists the
    database init, both agents and the serve route as actions, and `status`
    against that home reports all four as drift; `doctor` against a fixture
    with a corrupted database, an unloaded runner, a stale heartbeat, and a
    missing route names each, asserted with stubbed `launchctl`,
    `tailscale` and `curl`. A test seeds
    rows past every retention age and a backup that passed, runs the
    prune, and asserts the counts removed match the seeded excess, a
    `report` row carries them, and a kept worktree seeded fifteen days old
    is still present and counted stale on Today; with the night's backup
    failed, the prune refuses and removes nothing; an `exec` note older
    than ninety days survives the prune with its entry, arguments and
    exit code, marked `output expired` with its file gone, asserted from
    the command log; a `cost` row seeded fourteen months old on a
    `blocked` assignment whose budget is spent survives the prune, the
    assignment's next call is still refused with `budget spent`, and its
    item's total on the item screen is unchanged. A test
    unsets one enabled entry's variable and asserts `doctor` names the
    entry and prints no value, asserted by grepping its output for the
    fixture key. A test asserts the backup directory is under a source path
    in `backup-verify.conf` and that `sd-db-backup` writes there.The kept-worktree bundle tests moved to item D,
    criterion 5, on 2026-09-05.

23. The harness. `sd_db.testing` is importable from both repositories' test
    suites, asserted by a test in each; a grep of both suites for a second
    GitHub double or a patch of `subprocess` around `launchctl`,
    `tailscale`, `curl` or `caffeinate` returns nothing outside the
    package; the fixture remote refuses a merge whose `sha` is not the pull
    request's head and records the refusal; and the harness's pull request
    is merged before any that claims a criterion naming it, asserted from
    the log. A source retires beside its replacement: a test runs the
    `docs/work` migration against a fixture repository whose `sd-ship`
    still writes the frontmatter line, and asserts that the import lands the rows,
    retires nothing, and that a status change made through the old
    command after the import is read back by the next import; a test
    runs the same migration's retire step with a verify difference seeded
    and asserts the freeze is lifted and the source unchanged; and the
    retire step of each source runs in a pull request after the one that
    lands its writer, asserted from the log as the harness is.

## Open questions

1. **GitHub spend.** Probed 2026-09-05. The personal bill is reachable:
   `GET /users/<login>/settings/billing/usage?year=&month=` answers with the
   token the MCP server holds, which carries the `user` scope; the `gh` token
   does not. One row per product, SKU and day, with `quantity`, `netAmount`
   and no repository attribution for Copilot. The August 2026 rows carry a
   real net amount, the number the operator worried about. The organisation
   bill is not reachable, because the operator is not an owner.
   So: a nightly job writes the personal rows as `meter` cost rows on a
   `github` bill, the Today tile shows the month's net beside the other
   bills. The organisation's spend is not a gap: the operator said on
   2026-09-05 that every Copilot dollar goes through the personal account,
   on a Copilot Max subscription, so the personal rows are the whole
   number, the August net is that subscription's overage on premium
   requests, and the `github` bill is a subscription with a metered
   overage, shown as spend against the budget GitHub holds. Copilot review
   rounds per week from `shadow` stay as a count and stand in for nothing. The one
   control over the personal number is GitHub's own budget setting on
   premium requests, which the operator sets in GitHub billing; the pack can
   only stop asking, which requirement 4 of item A already does.
8. **"The usage read."** Settled 2026-09-05, see requirement 6: it is the
   total a `start` session reports at its own exit, read and written by the
   thing that started it, one `run` row per session with the assignment and
   the pass on it. B's round five found the phrase carrying three clauses
   and naming no mechanism anywhere on the page, which put the writer in a
   different pull request under each reading and blocked PR 8. The three
   readings weighed and rejected: the vendor's billing endpoint and the
   `local-agent-meter` sampler are both provider-wide with no assignment,
   so neither can satisfy clause 15.13's `assignment` and `pass`; and
   `sd usage` may never be run, so rows would simply be absent. Per-call
   rows were rejected with them — a `start` entry cannot sit on a capped
   bill, so there is no cap to itemise for, and every downstream number
   reads a sum. The writer is **item D's runner**, D's PR 7, which already
   writes the row's cost from this item's library function. PR 8 keeps the
   schema, the `url` path and the sweep. Round thirty-four withdrew the
   session bound that was the phrase's original context and left the phrase
   standing; this closes it.

2. **Dashboard stack.** Settled 2026-09-05, see requirement 5: server-rendered
   from the Python server, one hand-written stylesheet, one vendored
   JavaScript file for in-place updates, no build step. A JavaScript front end
   with a bundler was the alternative; it buys nothing the five sections need
   and brings a build convention into a repository that has none. Overturn
   before section 1 lands or not at all.
4. **Palette execution.** Settled 2026-09-05: the palette runs command lines,
   see requirement 5. Copy-only was the recommendation, because execution
   makes the dashboard a place where an HTTP request runs a command on this
   Mac, and the tailnet puts every device and browser on it in reach. The
   operator chose execution; the guards in requirement 5 are the condition.
3. **Library packaging.** Settled 2026-09-05: the pack's installer provisions
   `sd_db` into the pack's virtualenv from this repository's checkout, as
   a built copy at the checkout's tag and never editable, from B's
   round forty-five. One
   installer, one place that knows the path. Item A's criterion 13 carries
   it.
5. **Charts.** Settled 2026-09-05, after round thirty-two: server-rendered
   SVG, the operator's decision, see requirement 5. A vendored chart
   library was the alternative; it is a second script under the CSP and a
   drawing the tests cannot read.
6. **The phone.** Settled 2026-09-05: the operator approved the iPhone set
   in requirement 5, Today, the Backlog list, the Item screen and the
   palette; the board, the matrix and the charts wait for the iPad to be
   done.
7. **A default budget.** Settled 2026-09-05: none. `budget_usd` exists on
   a row only when the operator typed it on the run dialog, requirement 5
   and item D's requirement 2; the standing bound is the bill's cap.

## Log

- **2026-09-05** — Item opened on `feat/one-database-one-front-door`, as item
  B of the three that came out of the second interview on the pack's item
  `2026-09-05-the-pack-runs-a-team-process-for-one-person`. Decisions carried
  in: the database at `~/.local/share/sd/sd.db` owned by this repository; state
  in the database, artifacts in git, the status line a derived mirror; one
  library, two faces; shadows read-only with notes beside them; Obsidian out of
  process, in as a knowledge base; the runner after the library and the Today
  screen; unattended merge as a per-repository policy the operator sets, never
  derived; five dashboard sections with buttons and their commands; four
  weekly numbers and no caps; handoff on rows; a `herdr` wrapper. The first
  work item in this repository.
- **2026-09-05** — The operator asked for a screen that is modern, appealing
  and fun to use, with light and dark following the system setting.
  Requirement 5 gains the look-and-feel list: two palettes on
  `prefers-color-scheme`, in-place updates, a command palette that shows every
  action with its command, keyboard navigation, useful empty states, no
  decorative motion. Criterion 12 gains the tests, and the one check that is
  not a test: a week of use, five days of GET requests. Open question 2, the
  stack, is settled on the strength of the same request: server-rendered, one
  stylesheet, one vendored script, no build.
- **2026-09-05** — Decisions carried in from item A's registry work: the
  dashboard binds to localhost with no authentication, this Mac only; cost
  rows carry
  bill, tokens and dollars; the capped provider bill, `baseten`, has the first
  cap, fifty dollars a month, and the library enforces it.
- **2026-09-05** — Three more asks: raise the cap from the page, pick the
  provider from the page, and a usage report by vendor. Consequence: the
  registry file keeps identity and seeds two new tables, `provider` and
  `bill`, which hold what the page changes: enabled, order, caps. Ten tables.
  System gains the providers-and-bills screen and the usage report; the item
  screen gains review-now with a picked provider; Today's cost tile is one
  line per bill against cap. Criteria 1, 13 and 15.
- **2026-09-05** — Reach reversed: the operator works mostly from an iPad,
  so the dashboard is reachable over Tailscale. The server binds the tailnet
  address only, identifies the peer with `tailscale whois`, serves one login,
  and takes writes only same-origin. Touch-first layout, home-screen install.
  Open question 4 restated with the wider reach in view. Criterion 12.
- **2026-09-05** — Open question 4 settled by the operator: the palette
  executes command lines. Recorded against the recommendation, with the
  guards as the condition: listed entries only as fixed argument vectors, no
  shell, tailnet identity, same-origin, a login-bound session token, and an
  `exec` note per run. Requirement 5, criterion 12.
- **2026-09-05** — The operator asked for the allow-list and the token by
  name. Both made explicit: `commands.yaml` is the allow-list, with typed
  placeholders and a System view; the token is login-bound, rotated daily,
  and carried as a strict cookie plus a matching header. Requirement 5,
  criterion 12.
- **2026-09-05** — The operator asked for a log to review the commands. The
  command log joins System: every `exec` note with device, login, entry,
  values, item, exit code, duration and output, filterable, never deleted
  from the page, with the live allow-list beside it. Requirement 5,
  criterion 12.
- **2026-09-05** — Planning review, round one, `sd-review --scope planning
  --challenge` through Codex. Three blocking findings, all addressed.
  - C-1, requirement 1: the sole authoritative store had no recoverable
    backup; the clone cannot copy a live WAL file consistently and
    `local-backup-verify` samples only files unchanged for seven days.
    Addressed: nightly `sd-db-backup` with `VACUUM INTO`, thirty dated files
    the clone and the verifier cover, a restore test on every run that finds
    a row committed that day, and no migration retires a store before the
    first restore has passed. Criteria 1 and 5.
  - C-2, requirement 1: writing the mirror on every status change was a dual
    write across SQLite and a checkout with no recovery protocol, and a
    checkout need not exist. Addressed: a status change writes the row only;
    `sd-plan` and `sd-ship` write the mirror in the checkout they run in; the
    lint reports a stale mirror with its repair and does not fail. Criterion
    7, and item A's requirement 5 and criterion 13 to match.
  - C-3, requirement 6: `cost` rows had no attribution key, and the meter's
    provider-wide samples cannot be split between assignments. Addressed:
    `cost` gains `assignment` and `source`; `run` rows come from the thing
    that ran with its own token counts; `meter` rows feed plan usage only;
    per-item cost sums `run` rows. Criterion 15.
- **2026-09-05** — Operator answers: exec output kept ninety days under
  `~/.local/share/sd/exec/`; the pack's installer provisions the library,
  open question 3 settled; model pins and prices for the prepaid and capped
  entries come later, entries stay disabled until then; the GitHub spend
  source and the prepaid balance endpoints are probed by the agent and the
  answer recorded here.
- **2026-09-05** — Probes. GitHub: personal billing usage reachable, with an
  August net on Copilot AI Credits, organisation not reachable; open
  question 1 settled with the nightly `github` meter rows and GitHub's own
  budget as the control. Moonshot: `GET /v1/users/me/balance` on
  `api.moonshot.ai` answers with `available_balance` for the Kimi key, so the
  Usage screen shows the prepaid balance for that bill. MiniMax: no balance
  endpoint in its API; the dashboard is the only place, so that bill shows
  spend from `run` rows only. Balance rows are `meter` rows with a
  `balance` unit on the bill.
- **2026-09-05** — Planning review, round two, same lane. Three blocking
  findings, all addressed. This is the last remediation round the contract
  allows.
  - C-4, requirement 1: a backup taken before an import would have
    authorised retiring the import's source. Addressed: a source stays until
    a snapshot taken after the import restores with that migration's row
    count, and the retirement step refuses without one. Criterion 5.
  - C-5, requirement 4: the runner treated a branch name and its own
    serialisation as isolation, while interactive sessions and restored
    `herdr` agents write to the same checkout. Addressed: one worktree per
    item under `~/.local/share/sd/worktrees/`, reused for the branch, refused
    when the branch is checked out elsewhere, never the operator's checkout.
    Criterion 9.
  - C-6, requirement 7: notes had no resolution state, so finished followups
    would return to every brief. Addressed: `resolved_at` on `note`,
    `sd note resolve`, the same button on the item screen, open means
    unresolved, resolved stays in history. Criterion 16.
- **2026-09-05** — Planning review, round three. Three blocking findings,
  all addressed. The operator raised the contract to five automatic rounds
  in the same hour, so a fourth follows.
  - C-7, requirement 2: sources kept their writers between import and
    retirement, so a cron or a session could change a source row the rows
    no longer matched, and the count check would not see it. Addressed:
    every migration runs freeze, import, verify by identity and content,
    snapshot, retire, in that order; a run before the freeze is a
    rehearsal. Requirement 3's jobs stop as the freeze. Criterion 5.
  - C-8, requirement 2: retiring the pack's `dashboard` package removed the
    only GitHub collector, freezing `shadow` on the day of the switch.
    Addressed: the collector moves into the library as `sd shadow sync` with
    its watermark, a nightly job runs it, and the old package retires only
    after a post-import sync has brought in a new issue and a changed state.
  - C-9, requirement 6: a threshold on completed cost lets concurrent calls
    pass the cap together. Addressed: the library reserves a bound per call
    in one transaction on the bill's `reserved_usd`, refuses when spent plus
    reserved plus bound would pass the cap, and reconciles to actual cost
    after. Criterion 15.
- **2026-09-05** — Operator decision: TaskNotes and `obsidian-tasks-nightly`
  stay until the new system has proven itself; the operator retires them
  explicitly. The migration is written but not run; six vault jobs stop, not
  seven. Requirements 2 and 3, criterion 8.
- **2026-09-05** — Planning review, round four. Two blocking findings, both
  addressed. A fifth round follows, the last automatic one.
  - C-10, requirement 6: releasing a reservation on timeout assumed the
    provider stopped billing when the client stopped waiting, so a finished
    call with a lost response would admit another call against room it had
    already used. Addressed: a lost response converts the reservation into a
    `source: bound` cost row at the full bound, listed on the usage screen
    for correction against the invoice; nothing else settles a reservation.
    Criterion 15 tests the failure case instead of the release.
  - C-11, requirement 4: the queue had no crash-recovery protocol, so a
    restarted runner could strand a running assignment or dispatch a second
    agent into its worktree. Addressed: the runner claims the row with its
    pid and the child's before dispatch, children die with the runner's
    process group, and on start every `running` row becomes `blocked` with a
    `runner restarted` note until the operator requeues it. Criterion 9.
- **2026-09-05** — Planning review, round five, the last automatic one. Two
  blocking findings, both addressed. The contract allows no sixth automatic
  round; the operator decides whether to run one by hand.
  - C-12, requirement 6: the reservation was an aggregate on the bill row,
    so a process killed after reserving left no call identity to settle, and
    the cap either stayed exhausted or freed money already billed.
    Addressed: a reservation is a `cost` row of `source: reserved` with the
    call id, bound and owner pid; settlement is one idempotent transition by
    call id to `run` or `bound`; orphans are settled to `bound` by the next
    reservation and by `sd usage`; `bill.reserved_usd` is gone. Criterion 15.
  - C-13, requirement 6: the runner, `sd-review` and the API client each
    wrote `run` rows, so a review inside an assignment could charge one call
    twice. Addressed: the library's `charge` is the one writer, one row per
    provider call keyed by call id, with `assignment` and `pass` as context;
    totals are sums. Criterion 15 tests the nested case and greps the other
    two for cost inserts.
- **2026-09-05** — Operator requests, recorded after the last automatic
  round and not reviewed by the lane. Requirement 5 gains a review action on
  every skill, installed, on trial or in `contrib/`: one reviewer pass with a
  skill-review lens, recommendations as `proposal` notes on a `skill-review`
  item, accepted ones applied as one assignment and one pull request;
  `sd skill review <name>` from the terminal; criterion 19. Every list gains
  the same filter, selection with the section's bulk actions, and pages of
  fifty; criterion 12. Item A's requirement 10 names the third button.
- **2026-09-05** — Planning review, round six of fifteen. Two blocking
  findings, both addressed.
  - C-14, requirement 2: the idea import wrote the ladder word into a
    six-value status, so `inbox`, `accepted`, `drafting`, `approved` and
    `published` either broke the schema or lost the distinction the writing
    flow runs on. Addressed: `item.stage` keeps the word verbatim, one
    declared table derives `status`, total over both manifest ladders, and
    the import refuses an unnamed stage. Criterion 5.
  - C-15, requirement 6: "shipped" meant "reached `done`", so 95 declined
    ideas and every closed report counted as deliveries. Addressed:
    `item.shipped_at`, set once by the thing that delivered, and the four
    numbers read it; closure never sets it. Criterion 15.
- **2026-09-05** — Planning review, round seven of fifteen. Two blocking
  findings, both addressed.
  - C-16, requirement 1: once report rows replaced email, the database was
    the only channel for reporting its own loss. Addressed: a failed backup
    or restore, an unopenable or unwritable database and a report job that
    cannot write its row each send one email through the cron mail path,
    which stays as the named exception to requirement 5; criterion 1 tests
    it with the database unopenable and the volume full, criterion 14 names
    the exception.
  - C-15b, requirement 1: the restore check demanded a row from the last
    twenty-four hours, so a quiet or fresh installation failed a good
    backup. Addressed: the job writes a checkpoint row before the snapshot
    and the restore checks that row plus per-table counts; criterion 1
    covers an empty database and one with only old rows.
- **2026-09-05** — Carried from item A's planning review, round four,
  finding C-11 there: a branch name is not a revision identity. The `item`
  row gains `rev`, the commit that last wrote the mirror, and a mirror write
  refuses when `HEAD` does not contain it. Requirement 1.
- **2026-09-05** — Planning review, round eight of fifteen. Two blocking
  findings, both addressed.
  - C-17, requirement 7: the hook injected every open note of every item in
    the repository, with no bound and no exclusion of `done` items, so
    history grew into every new session. Addressed: a bounded brief, open
    followups and questions of the checked-out item or of the repository's
    not-`done` items, cut at eight kilobytes with the count and the listing
    command; other kinds never inject. Criterion 16.
  - C-18, requirement 1 and 5: the note vocabulary lacked `exec`, and the
    record was written after the command. Addressed: `exec` joins the kinds
    with its fields; the note is written before dispatch and completed
    after, an interrupted run shows as such, and a command whose note cannot
    be written does not run. Criterion 12.
- **2026-09-05** — Planning review, round nine of fifteen. One blocking
  finding, addressed.
  - C-19, requirement 5: the front door was plain HTTP on the tailnet
    address, and browsers send Fetch Metadata and `SameSite` cookies only to
    a trustworthy origin, so every write would have been refused while every
    page rendered, and the stubbed-header tests would not have seen it.
    Addressed: Tailscale Serve terminates HTTPS on the machine's `ts.net`
    name and proxies to the application on loopback; identity is the
    `Tailscale-User-Login` header Serve stamps, read only from a loopback
    peer; the cookie is `Secure`; one iPad write is checked by hand.
    Criterion 12. The earlier "never binds localhost" rule goes with it: a
    process on this single-user Mac is the operator's own, and the origin
    and token checks still apply.

- **2026-09-05** — Planning review, round ten of fifteen: two blocking
  findings, both addressed, and one operator request.
  - C-20, requirement 5: the palette ran commands in the item's repository
    from the server's own process, a second execution path outside the
    runner's worktree isolation and per-repository serialization, so two
    allowed commands, or a command and an assignment, could write one
    checkout at once, and a command could run on whatever branch the
    operator had checked out. Addressed: every entry declares `mutates`;
    read-only entries run in place, mutating entries become an `exec`
    assignment the runner executes in the item's worktree under the same
    serialization. Criterion 20.
  - C-21, requirement 5: a job's email stopped as soon as its report had a
    row, before anyone had shown the dashboard gets opened, so every
    actionable report would have lost its channel while every conversion
    test passed. Addressed: email stays until the adoption gate in criterion
    12 is recorded, is then retired job by job on the operator's ask, and a
    `report` row carrying `attention` mails forever as the second named
    exception. Criterion 14.
  - Operator request: multi-select on Backlog and Today with `Run
    sequential` and `Run parallel`. Requirement 4 gains chains by `after`,
    a `parallel` lane under `parallel_max`, one worktree per row, one serial
    merge lane per repository, and `budget_minutes` on every assignment,
    the parallelism doctrine the operator already runs by hand. Criterion
    20, schema row for `assignment`.

- **2026-09-05** — Planning review, round eleven of fifteen: two blocking
  findings, both addressed.
  - C-22, requirement 4: children ran in the runner's own process group, so
    a timeout that killed "the process group" would have killed every
    running assignment and the runner with them, and the timeout could not
    be recorded before the restart. Addressed: each child leads its own
    process group, recorded on the row; a timeout kills that group alone;
    the runner kills its claimed groups on a clean exit and, on start, any
    group a `running` row still names whose leader's start time matches.
    Criteria 9 and 20.
  - C-23, requirement 4: the parallel lane bypassed per-repository
    serialization with no exclusion against serial and `exec` rows, so a
    parallel author could start beside a mutating palette command in the
    same repository, and worktrees do not isolate the object store and refs.
    Addressed: reader-writer exclusion per repository, parallel authors
    share, serial rows, `exec` rows and the merge step take it alone, first
    queued holds the next turn. Criterion 20.
- **2026-09-05** — `parallel_max` dropped on the operator's question: the
  selection is the decision, and a cap on top of it second-guesses the
  person who made it. Every parallel row starts at once; the exclusion
  rules and the budgets are the only brakes. Requirement 4, criterion 20.

- **2026-09-05** — Planning review, round twelve of fifteen: two blocking
  findings, both addressed.
  - C-24, requirement 4: a parallel author held the repository shared and
    its own ship step needed it alone, so two auto-merge authors reaching
    the merge together each waited on the other until the budget blocked
    both. Addressed: an author always ends at pull-request-ready and
    releases the repository; under `auto` it leaves a `merge` row, which the
    runner takes alone in queue order. No row upgrades shared access.
    Criteria 10 and 20.
  - C-25, requirement 4: recovery killed a group only while its leader was
    alive with a matching start time, so a leader that exited ahead of its
    descendants left them writing in a worktree the operator could requeue
    into. Addressed: each child is a supervisor that exits only when its
    group is empty, so leader lifetime is group lifetime; when the leader is
    gone and members remain, requeue and worktree reuse are refused naming
    the pids. Criterion 9.

- **2026-09-05** — Planning review, round thirteen of fifteen: two blocking
  findings, both addressed.
  - C-26, requirement 4: the merge row's four side effects are remote and
    irreversible, and restart recovery requeued the whole row, so a runner
    that died after GitHub accepted the merge would have rebased and merged
    again against a merged pull request, or stranded a delivered item
    without closure. Addressed: the row records `phase` after each side
    effect, and a requeued merge row reconciles against the pull request's
    state and the closure commit's `Closes:` trailer before it acts.
    Criterion 10 kills after each side effect.
  - C-27, requirement 4: `after` pointed at the author row, which now ends
    before its merge row, so a successor became eligible while the
    predecessor's merge could still fail. Addressed: a successor waits on
    its `after` row and on every row that row spawned; under `manual` the
    author's end is terminal and the successor starts without the
    predecessor's change, said plainly. Criterion 20.
- **2026-09-05** — Operator decision: Dependabot pull requests get a
  Dependencies screen under System. The morning job keeps every rule in
  `ROUTINE.md` and writes `dep` rows instead of a log; held rows carry the
  bulk actions merge, close, ignore this major, rebase, snooze and assign;
  Today shows held count, oldest age, last run and a missed run; the pack's
  `sd-deps` goes on item A. Requirement 5, criterion 21, `item` kind `dep`.
- **2026-09-05** — Planning review, round fourteen of twenty: one blocking
  finding, addressed.
  - C-28, requirement 4: an empty process group was taken as proof that an
    assignment had stopped writing, while a descendant that called `setsid`
    was left out, and the worktree was then reused by the next row.
    Addressed: a worktree is fresh per row and never reused; at the row's
    end the runner saves the uncommitted diff as a patch, names the
    processes still working there, removes the worktree and only then
    releases the repository, so an escapee writes into an unlinked
    directory with no `.git`. Criterion 9 tests a `setsid` writer by
    relative and absolute path.
- **2026-09-05** — Planning review, round fifteen of twenty: one blocking
  finding, addressed, and two operator decisions.
  - C-29, requirement 4: removing the worktree was called containment, but
    a survivor using the repository's own path can still update refs or
    push after the release. Addressed: the runner fails closed; with any
    survivor it can see, a live group member or a process holding a
    directory or file under the worktree or the repository's `.git`, the
    repository is quarantined, nothing is dispatched into it, the worktree
    stays for inspection, and the release waits for the survivors to go.
    Criterion 9 tests a working-directory survivor and a `.git` survivor.
  - Cross-item, from A's round ten: the merge row's `sd-ship` re-asks the
    remote whether the repository is still the operator's alone and whether
    the default branch is protected; a no ends `ready_to_send`; the closure
    is a second pull request.
  - Operator decision: report email keeps going until the operator asks for
    retirement by name, once satisfied the framework works; the adoption
    gate is the earliest the ask is accepted. Requirement 5, criterion 14.
- **2026-09-05** — Planning review, round sixteen of twenty: two blocking
  findings, both addressed.
  - C-30, requirement 4: every finished worktree was removed with `--force`
    after saving a patch, and a patch does not carry untracked files or
    binaries, so a session interrupted mid-edit lost them. Addressed: only
    a clean worktree is removed; a dirty one is kept intact and named on
    the row with its file count, the next row on the item refuses naming
    it, and the operator discards or commits. Criterion 9.
  - C-31, requirement 5: dependency merges were a second merge authority
    beside the runner's lane, so a bulk action, the morning job and an
    `auto` merge could advance the default branch under each other.
    Addressed: a dependency merge is a `merge` row on its `dep` item, taken
    alone by the runner like every other merge; the morning job classifies
    and enqueues and merges nothing itself. Criterion 21 overlaps all three.
- **2026-09-05** — Planning review, round seventeen of twenty: three
  blocking findings, all addressed.
  - C-32, requirements 4 and 5: the local queue was treated as the remote's
    safety boundary, while `@dependabot rebase` and any push move a head
    without the runner. Addressed: every merge names the head it validated,
    GitHub refuses a moved head, and the row re-validates before trying
    again. Criteria 10 and 21.
  - C-33, requirement 4: a sibling parallel author inside a `git` command
    satisfied the `.git` survivor check and turned a successful row into a
    quarantine. Addressed: own survivors, group members and worktree
    holders, block the row; a stranger holding `.git` only delays
    exclusive access and is named on Today; siblings count for nothing.
    Criterion 9.
  - C-34, requirement 4: committing in a kept worktree did not free the
    branch, so the documented recovery still left the next row refused.
    Addressed: `resume` checks the kept worktree clean, removes it and
    requeues on a fresh one; `discard` as before. Criterion 9 tests both.
- **2026-09-05** — Two operator decisions: `protect main` is one dashboard
  action on a repository the operator owns, pull requests only, CI required,
  no required approvals, requirement 5 and criterion 13; and ninety minutes
  stands as the default `budget_minutes`.
- **2026-09-05** — Three decisions of the operator's, on this run's open
  questions.
  - Requirement 4: under `manual` a sequential successor waits for the
    operator's merge. The runner watches every `ready_to_send` pull request
    and, on merged, creates a `merge` row at phase `merged` that lands the
    closure; a pull request closed unmerged blocks the item and its
    successors. Criteria 10 and 20 assert it. The previous text started
    the successor without the predecessor's change.
  - The `reviewers` consent line is asked for once per repository at
    install, item A.
  - Requirement 5: Backlog gains a board by status and a matrix by urgent
    and important, derived from `due`, `ready_to_send` age and `attention`
    against `priority`; Today gains the runner board. One list component,
    three views. Criterion 12 asserts them.
- **2026-09-05** — Four charts, asked for by the operator, each answering
  one question that ends in a button on the same screen: the runner
  timeline on Today and Usage, the age histogram above the Backlog views,
  the cost burn with projection and the plan-window gauges on Usage, and
  the provider scorecard beside the rank on Providers. Cumulative flow
  waits on a status history the schema does not keep. Criterion 12 asserts
  each from seeded rows.
- **2026-09-05** — Planning review, round eighteen of twenty: one blocking
  finding, addressed. The round's first remediation script matched nothing
  and applied nothing, and the nineteenth run, codex timed out, reviewed
  the unremediated text; this entry lands the fix and round nineteen is
  rerun.
  - C-35, requirement 4: clean was `git status --porcelain --ignored`
    empty, which `__pycache__`, a virtualenv or `node_modules` fail on
    every ordinary run, so a finished author kept its worktree and its
    branch, the merge row never started, and commit-and-resume failed the
    same check. Addressed: clean is `--porcelain` without `--ignored`;
    ignored files are the repository's own disposables and go with the
    worktree. Criterion 9 adds the build-artifact case.
- **2026-09-05** — Planning review, round nineteen of twenty: two
  blocking findings, both addressed.
  - C-36, requirement 4: dispatch added a worktree on the item's branch as
    it stood, so a successor whose branch predated the batch authored
    against the old base and the chain's order promised a dependency it
    did not deliver. Addressed: before every session the runner fetches
    and rebases the item's branch onto the default branch; a conflict
    blocks the row naming the files. Criterion 20 asserts the successor
    sees the predecessor's file.
  - C-37, requirement 4: a crash between opening the closure pull request
    and merging it left no closure commit and no phase, so replay opened a
    second closure pull request or failed on the existing branch.
    Addressed: the closure branch is `closure/<item>`, the row reconciles
    against the pull request on that head, open, merged or absent, before
    it opens one, and `phase` gains `closure_opened`. Criterion 10 kills
    between opening and merge.
- **2026-09-05** — Planning review, round twenty of twenty, the last
  automatic round: two blocking findings, both addressed. The fixes below
  have had no review lane over them; a further round is the operator's to
  ask for by name.
  - C-38, requirement 4: the round-nineteen fix rebased the item's branch
    at dispatch, which rewrites the commit item A's `rev` names, so the
    mirror guard would refuse the successor's own `sd-ship` write after a
    clean rebase. Addressed: dispatch and the merge row merge the default
    branch into the item's branch and never rebase; the merge commit never
    reaches the default branch because `sd-ship` squash-merges. The merge
    row's first phase is `updated`. Criterion 20 asserts `rev` is still
    contained after the update.
  - C-39, requirement 4: the eligibility rule waited on the `after` row
    and the rows it had spawned, and under `manual` nothing was spawned at
    the author's end, so the successor was eligible before the operator
    merged, against the decision recorded the same day. Addressed: one
    delivery barrier under both policies, a `merge` row with `parent` the
    `after` row that ended `done`; an author's end is completion, not
    delivery; successors block in the transaction that records a failed
    end. Criterion 20 asserts one query serves both policies.
- **2026-09-05** — Requirement 9, asked for by the operator: the backbone
  learns the new pieces. Profile and doctor build and check the database,
  runner, dashboard and serve route; the runner writes a heartbeat that
  Today and `local-health-check` read; sessions run under `caffeinate`;
  one retention table pruned after a passed backup; the runner's plist
  sources the operator's environment file; the dated backups move under
  `~/Documents/sd-backups/`, inside a pair the verify already samples.
  Criterion 22 asserts each.
- **2026-09-05** — Planning review, round twenty-one of thirty: two
  blocking findings, both addressed.
  - C-40, requirement 4: whichever row was queued first held the next
    turn, so a successor queued before its predecessor's merge row existed
    held the repository against that merge row and the batch stood still.
    Addressed: only eligible rows hold a turn; a row waiting on its
    `after` reserves nothing. Criterion 20 runs the two-item case.
  - C-41, requirement 4: the merge watch covered `manual` repositories
    only, while an `auto` repository whose safety check answered no also
    ends `ready_to_send` and is merged by hand. Addressed: the watch covers
    every `ready_to_send` item whatever the policy. Criterion 10 adds the
    `auto` case.
- **2026-09-05** — Planning review, round twenty-two of thirty: one
  blocking finding, addressed.
  - C-42, requirements 4 and 9: a kept worktree holds the one copy of an
    interrupted session's work, under a path no backup pair covers, and
    requirement 9 called worktrees disposable while the dated database
    snapshot named that work as if it were safe. Addressed: keeping a
    worktree also snapshots it to `refs/sd/kept/<assignment>` and bundles
    that ref and the branch under `~/Documents/sd-backups/worktrees/`;
    `sd worktree restore` rebuilds it; a clean worktree is disposable, a
    kept one is bundled. Criterion 22 loses the directory and restores.
- **2026-09-05** — Planning review, round twenty-three of thirty: two
  blocking findings, both addressed.
  - C-43, requirement 4: `resume` removed the bundle on a clean worktree,
    and clean meant committed, not pushed, so a local commit made in the
    kept worktree was held by the disk alone until its push. Addressed:
    `resume` pushes and confirms the remote head before it removes the
    worktree and the bundle; a failed push keeps both; `restore` for a
    removed bundle reports the branch on the remote. Criterion 22.
  - C-44, requirement 4: the merge row merged the default branch into the
    reviewed head and went on to CI alone, so two parallel authors could
    each pass review and ship a combination nobody reviewed. Addressed: an
    update that changes the head is reviewed again on the new head before
    CI; a no-op update leaves the reviewed head standing; a blocking
    finding blocks the item. Criterion 20 asserts both cases.
- **2026-09-05** — Planning review, round twenty-four of thirty: one
  blocking finding, addressed.
  - C-45, requirement 4: the moved-head retry resumed from `ci_passed`,
    so a commit pushed after review and CI could merge on CI alone.
    Addressed: review and CI are recorded against the exact `sha` each
    passed on, a moved head invalidates both, and the retry starts from
    the branch review. Criterion 10 tests the blocked and the clean
    variant.
- **2026-09-05** — Planning review, round twenty-five of thirty: one
  blocking finding, addressed.
  - C-46, requirement 4: a clean worktree was removed without a bundle,
    and clean proved committed, not pushed, so a session that committed
    and died before its push left its commits on this disk alone.
    Addressed: one durability gate for every ended row, the head on the
    remote or pushed now, else kept and bundled as the dirty case is.
    Criterion 9 refuses the push and restores from the bundle.
- **2026-09-05** — Two operator statements and one cross-item change.
  The Copilot spend is entirely personal, on a Copilot Max subscription;
  open question 1 drops the organisation proxy and names the `github`
  bill a subscription with metered overage. From A's round fourteen,
  `protect main` sets four settings, branches up to date before merge
  among them. Criterion 13.
- **2026-09-05** — Planning review, round twenty-six of thirty: two
  blocking findings, both addressed.
  - C-47, requirement 4: ignored was taken for disposable, and this
    repository ignores `storage/`, `state/`, `volumes/`, logs and `.env`,
    so a session's output under one of those went with the `--force`.
    Addressed: disposable is an allow-list of build caches; every other
    ignored file the session made is moved under the backup path at its
    relative path before removal, named on the row, and the branch is
    still released. Criterion 9 writes a PDF under `storage/`.
  - C-48, requirements 1 and 9: `providers.yaml` and `commands.yaml`
    under `~/.local/share/sd/` were outside the backup, so a restore
    brought back rows and not the registry or the allow-list. Addressed:
    the nightly backup is a dated directory with both files beside the
    database, and `sd-db-backup restore <date>` into an empty home is
    tested. Criterion 1.
- **2026-09-05** — Planning review, round twenty-seven of thirty: one
  blocking finding, addressed.
  - C-49, requirement 1: a restored database restored permission to run
    what may already have run, an `exec` row `queued` again after its
    command had executed, and a cap reopened by a snapshot older than the
    spend against it. Addressed: restore fails closed; one `restore` row
    stops dispatch and the palette until `sd restore resume`; pending
    rows are blocked with a `restored` note; merge rows reconcile against
    GitHub; capped bills are frozen until the month's spend is read or
    typed. Criterion 1 snapshots before an execution and a charge.
- **2026-09-05** — Planning review, round twenty-eight of thirty: one
  blocking finding, addressed.
  - C-50, requirement 4: a failed `resume` push kept the bundle written
    when the row ended, so the commits the operator made in the kept
    worktree since were held by the disk alone. Addressed: the snapshot
    is one operation run wherever a worktree is retained, at the row's
    end, after a failed push, and nightly for every kept worktree.
    Criterion 22 refuses the push, deletes the repository, restores.
- **2026-09-05** — Planning review, round twenty-nine of forty: two blocking
  findings, one addressed here, one addressed on item D. The cap was raised
  from thirty to forty automatic rounds by the operator the same day.
  - C-51, requirement 5: artifacts, imported titles, notes and command
    output were rendered on the origin that holds the execution controls
    with no stated boundary. Addressed: escaped text by default, Markdown
    through a sanitizer that emits no executable HTML, a
    `Content-Security-Policy` with no inline script on every response.
    Criterion 12 renders a malicious artifact and command output and
    asserts nothing ran and no palette request left.
  - C-52, requirement 4: the delivery barrier waited for the closure pull
    request, so a closure whose CI failed blocked every successor although
    the code they needed was on the default branch. Moved with requirement
    4 to item D and addressed there: delivery is the confirmed merge, phase
    `merged`; the closure follows and its failure blocks nothing.
  - Operator's decisions, the same day: the runner is its own item, D,
    `docs/work/archive/2026-09/2026-09-05-the-runner-works-the-queue/`, and requirement 4,
    criteria 9, 10, 11 and 20, and the runner's backbone bullets and tests
    moved there, the pointers kept here; the fixture harness is requirement
    10 and the first slice; the three items land in the order under Landing
    order; each registry entry names its `env` variables; a batch shows its
    cost bound before it runs, requirement 5 and criterion 12; every status
    write leaves a `status_change` note, requirement 1 and criterion 7;
    an `attention` row pushes through `local-notify`, requirement 5 and
    criterion 14; `design.md` is the one-page reader's version of this
    item; the iPad is the reference device and the iPhone gets Today, the
    Backlog list, the Item screen and the palette, the rest postponed,
    requirement 5 and criterion 12. Item A's round seventeen dropped the
    up-to-date protection setting and the automatic integration update;
    item D followed, and `phase` lost `updated`, until round thirty below.
- **2026-09-05** — Planning review, round thirty of forty, run over this
  item and item D together, since `sd-review` reviews every planning item
  in the checkout at once: three blocking findings, one addressed here, one
  addressed on items A and D, one on item D as its C-1.
  - C-53, requirement 5: every mutating palette entry ran as an `exec` row
    in a fresh worktree of the item, and `sd worktree discard` and `resume`
    are the mutations a kept worktree needs while it refuses a fresh one,
    so recovery could not be done from the iPad. Addressed: entries carry
    `scope`; a `supervisor` entry runs in the runner's own process against
    the row it names and makes no worktree. Criterion 12 discards and
    resumes from the fixture browser.
  - C-54, requirement 5: `protect main` set the up-to-date requirement
    that item D, after A's round seventeen, assumed absent, so the
    dashboard's own action broke the planned parallel merge. Addressed on
    A and D, not here: the setting is back everywhere, the integration
    update is back and bounded, and `phase` has `updated` again. The
    pointer in requirement 4 follows.
  - D's C-1, on the text that was this item's requirement 4 until the
    split: two parallel authors that each pass alone can merge without a
    conflict and break the default branch, found after delivery. Addressed
    on D with the same restoration.
- **2026-09-05** — Planning review, round thirty-one of forty, with D's
  round two in the same prompt: two blocking findings, both on this item,
  addressed.
  - C-55, requirement 5: `supervisor` entries needed the repository alone,
    which is refused while an author runs and withheld during a
    quarantine, so kill and quarantine-clear, the recovery the iPad needs
    most, waited for the state they exist to end. Addressed: a third scope,
    `control`, acts on the runner's next tick in its own process with no
    exclusivity, touching no worktree and no ref; kill and clear are
    control entries, discard and resume stay supervisor. Criterion 12 kills
    a running author and clears a quarantine from the fixture browser.
  - C-56, requirement 5: the batch preview summed one author call per item
    and called it a bound, and an assignment makes many calls, reviews
    included. Addressed: with no budget the number is an estimate and says
    so; with `budget_usd` typed the library reserves every call of the
    assignment, nested reviews and the integration review included, against
    the row's budget on every bill, and the sum of budgets is the bound.
    Requirement 6, criteria 12 and 15; item D carries `budget_usd` on the
    row.
- **2026-09-05** — Planning review, round thirty-two of forty, with D's
  round three in the same prompt: two blocking findings, one here, one on
  D as its C-2.
  - C-57, requirements 5 and 6: `budget_usd` was enforced by the library's
    reservation, and a `claude -p` or `codex exec` session makes its own
    requests inside its own process, so a budget on such a row was a
    number the session could pass before the library saw a call.
    Addressed: a hard budget is accepted only where every call goes
    through the library, a `url` author; a budgeted batch on a `start`
    author is refused at the dialog naming the entry and offered the
    estimate. Criteria 12 and 15, the latter charging a `start` session
    from the usage read with no reservation.
- **2026-09-05** — Operator's decisions after round thirty-two, from the
  list of seven presented with A's round twenty: charts are server-rendered
  SVG, requirement 5 and criterion 12; the iPhone screen set stands as
  written; `budget_usd` has no default; item D's reviews pause until its
  spike has run, and the pack's `sd-review` gains `--item` so this item is
  reviewed alone from round thirty-three. Open questions 5, 6 and 7 record
  the first three.
- **2026-09-05** — Planning review, round thirty-three of forty, the first
  on this item alone: two blocking findings, addressed.
  - C-58, landing order: the first slice ran the migrations to retirement,
    so `docs/work` and the register were frozen or ignored until A's
    writers landed a slice later, an unbounded gap in which work was
    untracked. Addressed: the first slice imports and verifies and retires
    nothing, every source stays authoritative and written by what writes
    it today, and each source retires in the slice that lands its
    replacement, freeze to retire in one command run. Requirement 2, the
    landing order, criterion 23.
  - C-59, requirement 6: the cap's zero overshoot assumed the library
    reserves before every charge, and a `start` entry on a capped bill
    calls the vendor itself. Addressed: such an entry declares
    `session_bound_usd`, the library reserves it at session start against
    the same sum, settles it from the usage read, and an overshoot past the
    declared bound is the stated gap, written as a `cap overshot` attention
    row that pushes. Criterion 15; the field is item A's.
- **2026-09-05** — Planning review, round thirty-four of forty: one
  blocking finding, addressed, the same finding A's round twenty-one
  raised as its C-43.
  - C-60, requirement 6: `session_bound_usd` was a reservation and not a
    limit, since the session makes its own calls and a retry passes the
    declared bound with the money spent. Addressed by the simpler boundary
    the reviewer named: a capped bill takes `url` entries only, the
    registry reader and the dashboard's cap control refuse otherwise, and
    the session bound of round thirty-three is withdrawn. Requirement 6,
    criterion 15; today both `baseten` entries are `url` entries.
- **2026-09-05** — Planning review, round thirty-five of forty: two
  blocking findings, addressed, and one correction from A's round
  twenty-two.
  - C-61, requirement 1: status was free metadata from every surface, so a
    work item dragged to `done` left the queue undelivered and a page
    write raced the runner's lifecycle writes. Addressed: one `transition`
    function applies a per-kind table; a work item's `done` comes from a
    confirmed merge or from `cancel`, which writes a `cancelled` note; an
    item with a `queued` or `running` assignment refuses status writes
    from every page and command, naming the row. Requirement 5's board
    snaps back; criterion 7 tries every surface.
  - C-62, requirement 1: the schema was frozen at ten tables while the
    checkpoint, the `restore` row, the tracker watermark and the runner's
    heartbeat had no table. Addressed: an eleventh table, `state`, holds
    those four kinds by name, the count rule is withdrawn for a rule
    against growth by convention, and criterion 1 greps the library for
    an insert into an unnamed table. Item D's heartbeat lives there.
  - Correction: round thirty-four's log says both Baseten entries were
    `url` entries; A's round twenty-two found them to be the `prism` and
    `gito` CLIs, `start` entries, and replaced them with one `url` entry,
    `baseten`. Requirement 6 says so now. Also fixed: requirement 1 still
    said `done` deletes the directory and never reaches a mirror, both
    overturned in item A on 2026-09-05.
- **2026-09-05** — Planning review, round thirty-six of forty: two
  blocking findings, addressed.
  - C-63, requirement 6: one reservation per call id, and no retry
    boundary, so an HTTP retry after the provider accepted a request whose
    response was lost could bill twice against one bound. Addressed: a
    call id is one attempt on the wire, the client's automatic retries are
    zero and asserted, a lost response settles to `bound` once, and a
    caller's retry is a new call with its own reservation. Criterion 15
    drops a response on the double.
  - C-64, requirement 5: the execution token travelled as an `HttpOnly`
    cookie and had to be repeated in a header, which no script can do.
    Addressed: the cookie stays `HttpOnly`; the header carries a second
    value, a keyed hash of the session rendered into the page's markup,
    which the server recomputes from the cookie. Criterion 12's refusals
    name each missing or wrong half.
- **2026-09-05** — Planning review, round thirty-seven of forty: two
  blocking findings, addressed.
  - C-65, requirement 1 and the landing order: a `queued` assignment
    locked its item's status, and the fourth slice creates assignments a
    slice before the runner that would end them, with no way back.
    Addressed: a `queued` row has no process, so the library's own
    `cancel` ends it and frees the item without a runner, from the item
    screen, the board and `sd assign cancel` [superseded 2026-09-16, owner decision (a): `sd assignments cancel` / `sd runner cancel`]; the refusal names it, and
    only a `running` row needs the control entry. Criterion 7 assigns and
    cancels with no runner in the fixture.
  - C-66, requirement 9: the prune deleted `exec` notes at ninety days,
    the only durable audit record of what the palette ran, while
    requirement 5 promised the row leaves only with its item. Addressed:
    the prune ages the output file and marks the note `output expired`;
    the note itself is never pruned. Criterion 22 prunes and reads the old
    command back.
- **2026-09-05** — Planning review, round thirty-eight of forty: two
  blocking findings, addressed.
  - C-67, requirement 5: kill and quarantine-clear waited for the runner's
    next tick, and a hung or dead runner with a child alive is the state
    they exist to end. Addressed: the dashboard's process acts on a
    control entry at once, from the row, behind the same checks, and a
    third control entry restarts the runner through `launchctl`; the
    runner only reads a lifted quarantine. Criterion 12 kills and clears
    with the runner stopped. Item D's requirement 2 and criterion 4 follow.
  - C-68, requirement 6: per-item accounting summed `run` rows only while
    the four numbers summed `run` and `bound`, so an assignment whose only
    response was lost showed no cost on its item and a cost on the week.
    Addressed: one shared query sums both, `bound` labelled estimated until
    reconciled. Criterion 15 drops a response and reads three surfaces.
- **2026-09-05** — Planning review, round thirty-nine of forty: one
  blocking finding, addressed.
  - C-69, requirement 5: the policy header carried no `frame-ancestors`
    directive and nothing required `X-Frame-Options`, so a page on
    another origin of the same tailnet site could frame an authenticated
    dashboard under an overlay, and a genuine click on a real control,
    cookie and execution token intact, would run a command or raise a
    cap. Addressed: `frame-ancestors 'none'` on the policy and
    `X-Frame-Options: DENY` beside it on every response. Criterion 12
    frames the dashboard from a second origin and asserts nothing renders
    and a click leaves no request.
- **2026-09-05** — Planning review, round forty of forty-five: one
  blocking finding, addressed. The cap was raised from forty to
  forty-five automatic rounds by the operator the same day.
  - C-70, requirement 9: `cost` rows were pruned at thirteen months while
    requirement 6 admits a call by summing an assignment's rows and the
    item screen reports an item by them, so a blocked assignment resumed
    after the prune could spend its budget again and an old item shipped
    later understated its cost. Addressed: `cost` rows are never pruned;
    they are the ledger, and a year of them weighs nothing. Criterion 22
    seeds an old row on a spent assignment and asserts the refusal and
    the total survive the prune.
- **2026-09-05** — Planning review, round forty-one of forty-five: two
  blocking findings, addressed.
  - C-71, requirement 5: every dependency merge re-checked all four
    conditions of `ROUTINE.md`, which reject a major and a repository
    with no CI, so the one-tap decision the list promised queued a row
    the runner refused again. Addressed: the row carries its authority,
    `routine` or `operator`; a `routine` row re-checks all four, an
    `operator` row the mechanical two plus every check that ran, and
    skips the two the tap decided. Criterion 21 merges a major and a
    no-CI row by tap and refuses a hand-written `routine` major.
  - C-72, requirement 1: `cancel` wrote `done` with no merge, and `done`
    reached the mirror only through a closure after a confirmed merge, so
    a cancelled item stayed open in every other checkout and in CI.
    Addressed: `cancel` lands item A's closure with the `cancelled` note
    and no delivery behind it. Criterion 7 asserts the closure in the
    fixture checkout; item A's requirement 5 and criterion 13 follow.
- **2026-09-05** — Planning review, round forty-two of forty-five: two
  blocking findings, addressed.
  - C-73, requirement 6: the cap counted the month's reservations, and a
    reservation made before midnight on the month's last day and
    dispatched after it sat in the old month's bucket while the new
    month's callers reserved the whole cap. Addressed: every open
    reservation counts against whichever month asks, and the settled row
    carries the month of the wire attempt. Criterion 15 reserves before
    midnight and dispatches after.
  - C-74, requirement 5: writing an `attention` row and sending its mail
    and push were separate side effects with no delivery state, so a
    process that died between them left an alert nobody would read.
    Addressed: the row carries two pending deliveries written in the
    same transaction, and a sender in the writing job, every report job
    and the dashboard's tick sends what is pending and marks it. Criterion
    14 kills the job between commit and send.
- **2026-09-05** — Cross-item change from A's round twenty-eight, C-55:
  `cancel` lands a cancellation closure of its own, cut from the default
  branch and touching the mirror's status line alone, only where the
  default branch holds the mirror. Requirement 1 and criterion 7 follow.
- **2026-09-05** — Planning review, round forty-three of forty-five: one
  blocking finding, addressed.
  - C-75, requirement 5 and the landing order: the morning job became
    enqueue-only while the runner that would take its rows lands two
    slices later after a spike, so the safe patches that merge on their
    own today would have stopped merging for as long as that took.
    Addressed: the job keeps its serial merger and writes `dep` rows
    beside it until one cutover commit in the fifth slice, after the
    runner's merge check passes, removes the merger and hands `dep`
    merges to the runner in the same change. Criterion 21 asserts both
    sides of the cutover.
- **2026-09-05** — Planning review, round forty-four of forty-five: one
  blocking finding, addressed.
  - C-76, requirement 1: a restore reconciled bill totals and left every
    assignment's `budget_usd` standing on the rolled-back ledger, so a
    budgeted assignment snapshotted before it spent, restored and
    requeued, was authorised to spend its budget again. Addressed: the
    restore marks every unfinished budget spent with a `budget restored`
    note, a requeue takes a new budget the operator types, and the
    library refuses a reservation against a marked budget. Criterion 1
    snapshots, spends, restores and requeues.
- **2026-09-05** — Planning review, round forty-five of forty-five, the
  last automatic round: one blocking finding, addressed. The first run
  of this round timed out in the provider and was rerun.
  - C-77, requirement 1: the library was installed editable and applied
    migrations on open, so a branch switch in this checkout could
    migrate the one live database under the dashboard and the jobs, and
    switching back did not undo it. Addressed: a built copy at a tagged
    version in each virtualenv, migrations by one explicit command run
    with the processes stopped, a version check on open that refuses a
    newer database and refuses to write an older one, and `doctor`
    naming the three versions. Criterion 1 asserts each; open question 3
    follows.
- **2026-09-05** — Cross-item change from A's round thirty-one, C-61:
  there is no closure pull request; `done` reaches a mirror with the next
  mirror-refreshing commit item A's commands make in the repository,
  which carries `Closes:` for it. Requirement 1 and criterion 7 follow.
- **2026-09-05** — Cross-item change from A's round thirty-three and the
  operator's decision: the `status:` line leaves `prd.md`, read into the
  row and removed by requirement 2's migration; no mirror, no `rev`, no
  guard, no refresh, no closure. Requirement 1, the `item` row, the
  landing order, criterion 7 and the migration test follow.
- **2026-09-05** — Cross-item change from A's round thirty-four, C-64:
  the `docs/work` migration reads every item outside the archive, lands
  a `done` one as an unmarked `done` row, and its own pull request marks
  each with `Closes:`; the mark rides the pull request body and the
  merge message. Requirement 1, criterion 7.
- **2026-09-05** — Cross-item change from A's round thirty-six, C-67:
  the `docs/work` migration's retire step runs after item A's reader and
  ship path have landed, in a pull request of its own, and refuses under
  a pack whose `sd_lib` has no `delivered`. Requirement 1, criterion 7.
- **2026-09-05** — Cross-item change from A's round thirty-seven, C-68:
  until the retire sitting the line is the record and every writer still
  writes it, the rehearsal rows are not, A's reader reads the line where
  there is one, and the sitting's final import carries a status changed
  between the slices into the row. Requirement 1, criterion 7.
- **2026-09-05** — Planning review, round forty-six of forty-eight, after
  the operator raised the cap by three: one blocking finding, addressed.
  - C-78, requirement 6: a reservation the sweep had settled to `bound`
    still let its owner send, so a caller paused past the timeout or
    across a month's end dispatched a charge the ledger had closed and
    had given another caller the room for. Addressed: the client claims
    the reservation just before the wire, `reserved` to `sending` with
    the attempt's month, and refuses to send otherwise; a `sending` row
    stays charged at its bound until its response or its loss settles
    it, and settles to `bound` at once only when its owner is dead.
    Criterion 12 suspends the caller at both points, across the timeout
    and the month's end.
- **2026-09-05** — Cross-item change from A's round thirty-eight, C-70:
  the `repo` row gains `status_source`, `file` until the `docs/work`
  sitting sets `row`; from `row` a line anywhere is stale and ignored
  and the old writers refuse. The `repo` table, requirement 1,
  criterion 7.
- **2026-09-05** — Planning review, round forty-seven of forty-eight: one
  blocking finding, addressed.
  - C-79, requirement 6: the assignment budget summed `run`, `bound` and
    `reserved` and omitted `sending`, so a call on the wire at eight of a
    ten budget let a concurrent nested review reserve eight more.
    Addressed: exposure is one sum by one function for cap and budget
    alike, `run` and `bound` plus every open `reserved` and `sending`
    row. Criterion 12 holds a call in `sending` and refuses the second.
- **2026-09-05** — Planning review, round forty-eight of forty-eight, the
  last automatic round: one blocking finding, addressed.
  - C-80, requirement 6: a call claimed before midnight and sent after
    settled into the earlier month and released the later month's whole
    cap to money the later month owed. Addressed: a row whose settlement
    falls in a later month than its claim carries both months and counts
    against both caps until the operator settles it to one against the
    invoice on the usage screen. Criterion 12 claims before midnight and
    settles after.
- **2026-09-05** — Planning review, round forty-nine, one confirmation
  round granted by the operator past the cap: two blocking findings,
  addressed, and one cross-item change from A's round forty-one.
  - C-81, requirement 2: the `docs/work` freeze was a read-only note,
    which stops no session already running and no other checkout, so a
    status written between the final import and the switch was ignored
    and its source retired. Addressed: the sitting's first transaction
    sets `status_source` to `retiring`, every status writer reads the
    value in the transaction that writes and refuses under it naming
    the sitting, the database's write lock orders the two, a verify
    difference sets `file` back, and writers git alone can reach are
    fenced by the conflict at their merge and the lint. Criterion 7
    writes from a second checkout during the sitting.
  - C-82, requirement 6: an expired or orphaned `reserved` row settled
    to `bound`, though the mandatory claim proves such a row never
    reached the wire, so an interrupted process spent cap and budget on
    a request never made. Addressed: an unclaimed reservation is
    released, deleted in the transaction a claim runs in; `bound` is a
    `sending` row's settlement alone. Criterion 15.
  - A's C-75: the retire commit adds the tracked marker
    `.status-source` under the retiring repository's `docs/work/` beside removing the lines. Criterion 7.
- **2026-09-05** — Item C created, cross-item: the idea import's stage
  table named the tips ladder's `ready` and `approved`, words no Blog
  Ideas or Topics note carries, and no Topics word, so the Topics import
  would have refused all eleven notes. The table is now per ladder, Blog
  Ideas and Topics, and C owns it in the writing manifest; C also puts
  the two kinds on the database as `sd store`'s driver. Requirement 2.
- **2026-09-05** — C's round five, cross-item: the `docs/work` sitting
  removed the lines and then set `row`, so a run that died between the
  two left the repository `retiring` with nothing to import. `row` is
  set before the removal, every step idempotent, the same command
  reruns. Requirement 1, criterion 7.
- **2026-09-05** — Planning review, round fifty of fifty-four: one
  blocking finding, addressed, and one cross-item change from C's
  round six.
  - C-83, requirements 1 and 9: the sitting's snapshot preceded `row`
    and the removal, so a restore of it after the cutover left the
    repository `retiring` with no line to import and every writer
    refusing. Addressed: the restore reconciles source authority
    against the checkout's marker and never from the snapshot, setting
    `row` where the marker is present, since the pre-cutover rows are
    the verified ones and need no line; every sitting takes a cutover
    snapshot after its commit. Criterion 7 restores both snapshots into
    the retired fixture. C's sitting adds a marker for the same reason.
  - C's C-12: the `repo` row carries `pieces_source` beside
    `status_source`, C's sitting's own, because the two sittings are
    scheduled apart.
- **2026-09-05** — Planning review, round fifty-one of fifty-four: one
  blocking finding, addressed.
  - C-84, requirement 9: the restore promoted any restored `file` or
    `retiring` row to `row` on the checkout's marker alone, and a
    nightly snapshot from before the final import holds rehearsal rows
    the marker says nothing about. Addressed: the verify writes a
    `verified` state row, repository, kind and content hash, before the
    sitting's snapshot; the restore promotes only when the snapshot
    carries it, otherwise leaves the row `retiring` with a note and
    `sd restore reimport <repository>` reads the lines from the marker
    commit's parent, verifies and sets `row`. Criterion 7 restores a
    pre-import snapshot. C's marker passage carries the same.
- **2026-09-05** — Planning review, round fifty-two of fifty-four: clean,
  no finding. One cross-item change from C's round eight: a sitting on
  a source that lives in git reads `HEAD`'s tree and refuses on a dirty
  tree, so the marker commit's parent is what the verify saw.
  Requirement 1, criterion 7.
- **2026-09-05** — Planning review, round fifty-three of fifty-four: one
  blocking finding, addressed.
  - C-85, requirement 4: every report job and the dashboard tick sent
    each pending delivery and marked it after, so two senders on one
    row sent it twice. Addressed: a sender claims the delivery in one
    transaction, `sending` with pid and a minute's lease, before it
    sends; a dead or expired claim is retaken. The criterion runs two
    senders on one row and kills one inside its send.
- **2026-09-05** — Planning review, round fifty-four of fifty-four: one
  blocking finding, addressed.
  - C-86, requirement 1: the sitting read the checkout's `HEAD` tree,
    and an item lives on its branch until its merge, so a branch-only
    item was omitted and a newer line on a branch lost to the default's.
    Addressed: the sitting fetches and reads every branch that carries
    an item, refuses naming an item whose branches disagree, records
    `source_commit` on each row, and `sd restore reimport` reads from
    it. Criterion 7 runs a branch-only item and a divergent one. A's
    retire passage carries the same.
- **2026-09-05** — Cross-item change from C's round thirteen: the `item`
  row gains `fields`, the declared fields of a kind a pack's manifest
  describes, and `body`, its sections, for `idea` and the writing pack's
  `topic`, so that an idea filed after C's sitting, which has no vault
  note, has one owner for its rating and its text. Schema table only.
- **2026-09-05** — Cross-item change from C's round thirty-two, C-53:
  a source whose marker's one line is a commit has `sd restore
  reimport` read its lines from that commit, since a snapshot older
  than the verify carries no row that could name it. Requirement 1,
  criterion 7. No review round.
- **2026-09-05** — Planning review of the implementation artifact set, the
  round that follows `implement.md` being written. One concern, one lane,
  the host's own; the pack defines no second lane.
  - C-87, requirement 2: `design.md`'s section 2 and this page's
    requirement 2 named different migration sources. The page names five,
    `index.sqlite` to `shadow`, every `docs/work/*/prd.md`, the
    decision register's open entries, the vault's Blog
    Ideas and Topics, and the two open GitHub issues; `design.md` named
    "the vault backlog, the work-item directories, the report emails, the
    cost spreadsheets", which adds two that are not migrations and drops
    three that are. The first `implement.md` drafted its slice-1 scope
    from `design.md` and inherited the wrong set, leaving `sd shadow
    sync` unscheduled although a later pull request gates a retire on it,
    and leaving the register and the GitHub issues unscheduled entirely.
    Addressed in `implement.md`: PR 3 carries the five sources as a table
    with each count re-checked, and names `sd shadow sync` and its
    nightly job as its own work; PR 5 states the register retires in A's
    slice 2 and that nothing here schedules it. `design.md` is the
    condensed page and this one is right when they differ, which is what
    that page says of itself, so the page is not edited and the ledger
    records why the drift was able to propagate: the condensed page was
    read in place of the requirement.
    Counts re-checked against the machine on the day: `index.sqlite`
    holds 1,175 rows in `issue`, exactly as recorded; `docs/work` holds
    eight active items in the pack and four in the register's repository,
    exactly as recorded. The register, the vault and the GitHub issues
    were not re-checked, each being outside every repository this item
    touches, and `implement.md` marks them so rather than implying they
    were.
    Severity: material, not blocking. Nothing in the design is wrong; a
    plan built from it was incomplete, and the plan is what changed.

- **2026-09-05** — Adversarial planning review of `implement.md`, one lane,
  fourteen findings: five blocking, six material, three minor. The lane was
  a read-only reviewer given the `prd.md` as the authority and the
  filesystem as the check. The findings are addressed in `implement.md`
  except where noted; the ones that reach this file are recorded here.
  - **C-88, and a correction to C-87's own verification.** The C-87 entry
    above records "`docs/work` holds eight active items in the pack and four
    in the register's repository, exactly as recorded". That is false, and the
    method sentence in requirement 2 says why it was never checkable that
    way: the enumeration is across every repository that has the directory,
    and the check that produced "exactly as recorded" enumerated the two
    repositories already named rather than the filesystem. Counted properly
    on 2026-09-05 there are items across six repositories, most of them in
    one repository neither count named. The entry above is left as
    written, because a ledger that edits its own false claims is worth less
    than one that records them; this entry is the correction. Severity:
    blocking, and the source count in requirement 2 is corrected with it.
  - **C-89 addressed, criterion 6 was unbounded and item D breaks it.** The criterion
    read "every `docs/work/*/prd.md` on the machine". Item D's runner clones
    a registered repository's whole working tree onto the work volume, so
    those files are on the machine, carry no `item` row, and fail the
    criterion for as long as the retention holds the clone — a criterion
    failing on a runner doing its job. Bounded to the repositories the
    `repo` table holds, which keeps requirement 2's intent, since the table
    is a registry the system fills and not a list kept in a document. A path
    under the worktrees directory is never enumerated, and a test asserts it.
    Severity: blocking. The operator chose the bound on 2026-09-05.
  - **Operator's decisions of 2026-09-05, no review.** A repository the
    operator does not own, whose items the operator alone authored, is in
    scope: its items import and its files are retired like any other. The
    consequences are taken deliberately: its items appear in `sd.db` and on
    the dashboard, and item D's retention holds a frozen clone of its source
    for thirty days. The two open
    GitHub issues are shadowed instead of imported and closed, so nothing a
    reader of a shared repository can see is taken away. Most of its items
    predate the forty-five day threshold, so the migration seeds each row's
    idle clock from the file and not from the import.
  - **C-90, the two-repository split was inverted.** `implement.md` placed
    `sd_db` in `sd-ai-command-pack` and stated the dependency as "`system`
    imports the library and never the reverse". Requirement 2 says the
    opposite in three places: `local-sd-db/` in this repository holds the
    library; open question 3, settled, has the pack's installer provision it
    from this repository's checkout; and criterion 1 asserts `sd_db` imported
    from the pack's virtualenv resolves to a file outside this checkout,
    which is only true when the source lives here. Under the inversion the
    round forty-five property — that a branch switch or a pull in this
    checkout changes nothing a running process imports until the operator
    installs the next tag — is gone, and the `pip install` at a tag has no
    source to install from. Severity: blocking. Addressed: the split is
    reversed and `system` now lands first in every mixed slice.
  - **C-91, the dashboard was assigned to the package that is retired.**
    `implement.md` rebuilt the pack's `dashboard/`; requirement 5 rebuilds
    `local-project-dashboard/` here, and requirement 2 retires the pack's
    `dashboard` package and `bin/sd-dashboard`. The same page did both: one
    pull request built what a later one deleted. Severity: blocking.
    Addressed: every dashboard pull request is `system` and
    `local-project-dashboard/`.
  - **C-92, requirement 5 was compressed until work vanished.** Roughly four
    hundred and thirty lines of requirement plus criterion 12's hundred and
    eighteen sat in two pull requests of about twenty lines each, and the
    command log, `sd exec-log`, the Dependencies bulk actions, the three
    `scope` values, the shared list component and four of the five charts
    appeared in no pull request body at all. The mechanism was one closure
    table row absorbing all of it: criterion 12 was closed by a pull request
    described in its own body as having "no write control anywhere on the
    page", though the criterion is largely writes. Severity: blocking.
    Addressed: the dashboard is three pull requests, the item is nine rather
    than eight, and criterion 12 is closed across four of them plus three
    records a person writes. Requirement 6 was compressed the same way and
    is corrected with it.
  - **C-93, the hand-off count was wrong and the missing ones were
    cross-item.** `implement.md` said "two hand-offs leave this item"; there
    are six, and four of the four missing ones are criteria this item's own
    pull requests claimed — 19 and 21 need item D's runner, 16 needs item
    A's `SessionStart` hook, and the installer that makes the library
    reachable is A's criterion 13. The closing rule then set `status: done`
    on PR merges alone, so the item could close with four criteria marked
    closed and item D not started. Severity: material. Addressed: six
    hand-offs enumerated, and closing requires each to be closed on its own
    item.
  - **C-94, removing a job file without its profile entry.** Six vault jobs
    were removed from `local-cron-jobs/jobs/` and left in `personal.cron`,
    which reinstalls them; the two jobs this item adds were never added to
    it, so `sd-db-backup` would never load and `shadow` would freeze on the
    day of the switch. This is the `com.platypeeps.sdw-meter` lesson the same page
    quotes, one file over, with the sign flipped in one direction and
    unflipped in the other. Severity: material. Addressed in PR 9.
  - **C-95, `design.md` still carried C-87's error and two more.** The
    condensed page named the report emails and the cost spreadsheets as
    migration sources, which they are not, and omitted `index.sqlite`, the
    register and the GitHub issues; it named six of the eleven day-one
    tables; and its landing order collapsed slices 3 and 4, dropping
    requirements 6 and 9. C-87 corrected the copy and left the source, so the
    source produced two more wrong drafts. Severity: material. Addressed:
    the page is corrected this time, not just the artifact drawn from it.
- **2026-09-05** — Adversarial re-review, round two, of the rewritten
  `implement.md`. Thirteen findings: four blocking, five material, four
  minor. Round one's C-88 through C-95 all hold, and the reviewer
  re-enumerated the counts from the filesystem rather than from the
  document — the items across six repositories, and those past the
  forty-five-day threshold — and confirmed each. The defect pattern moved: out of the pull
  request scopes and into the closure table.
  - C-96, blocking: PR 1 claimed criterion 23's pack half — a test in the
    pack's suite importing `sd_db.testing`, and a grep of both suites. The
    pack reaches `sd_db` only through the installer, which is item A's
    criterion 13 in A's slice 2, and this page requires PR 1 to merge before
    every other pull request in every item. The claim was not merely
    misplaced, it was unsatisfiable. The grep half is pack work too: the
    `launchctl` and `tailscale` doubles it must find nothing outside the
    package are in `tests/test_sd_dashboard.py`, `tests/test_dashboard_actions.py`
    and `tests/test_sd_ledger.py` today. And `system` has no Python test
    suite at all. Addressed: PR 1 closes the `system` half; both pack halves
    are hand-off 7.
  - C-97, blocking: requirement 8's `local-herdr/` — the wrapper resuming
    each pane's agent session from its row — was in no pull request's
    Touches, and is not on disk. `git grep` finds the name only in this
    file. The closure table hid it as "PR 7's code", and PR 7 touches
    `local-project-dashboard/` and `local-sd-db/`, neither of which could
    hold a multiplexer wrapper. Addressed: PR 11, greenfield.
  - C-98, blocking: requirement 7's whole library surface — the note write
    API, `sd note resolve`, `sd note list`, the eight-kilobyte brief
    builder, Today's open-followup list and the item screen's per-note
    resolve button — was assigned to the cost pull request, whose body is
    `cost` rows, reservations, bills, exposure and the burn chart, and whose
    own verification says criterion 16 is not closed there. The words
    *note*, *followup*, *resolve* and *brief* appear nowhere in it.
    Addressed: PR 10.
  - C-99, blocking: PR 9's justification for editing `backup-verify.conf` —
    "until this line lands the dated directories are sampled by nothing" —
    is false. The file already carries
    `~/Documents|/Volumes/ccc/Users/<login>/Documents`, and
    `~/Documents/sd-backups/` is under it. That is precisely why
    `prd.md:1108-1114` chose the path: "inside a pair `backup-verify.conf`
    already has … with no new configuration". `design.md` agrees. Criterion
    22 asks for an assertion, not an edit. The page told the implementer the
    reverse of the requirement's own reason. Addressed.
  - C-100, material: twenty-five `prd.md` citations were off by twenty to
    twenty-three lines, every one of them past `prd.md:467`. They were
    computed against the revision before the criterion 6 rewrite of
    2026-09-05 inserted lines, and never re-resolved. `prd.md:711-713` for
    the hover rule landed on the Content-Security-Policy; `prd.md:816-822`
    for the charts landed on the `exec` note's write-before-run rule. The
    hardest form to catch: the lines exist and read plausibly. All
    corrected against the current file.
  - C-101, material: the title and PR 2 said two repositories while the
    hand-off list, the closure table and `prd.md`'s open question 3 all put
    the installer in item A. Every pull request here is `system`'s. The C-90
    reversal reached the ordering prose and not the scope line. Addressed.
  - C-102, material: PR 7 claimed criterion 13 whole. Its promote and demote
    clauses need Skills-screen writes no pull request has; its registry
    clauses are the Providers-and-bills writes, in no pull request body; and
    its last clause asserts behaviour in the pack. Addressed, with the three
    as hand-off 8.
  - C-103, material: PR 9's verification reduced criterion 22 to its first
    clause. It has nine. Addressed.
  - C-104, material, **decided by the operator on 2026-09-07.** Criterion
    12 required that "the repository contains no `package.json`". `git
    ls-files` returns `mezmo-webhook/package.json`, tracked in `system`
    today, so the clause could not pass as written. Resolved by narrowing
    the subject to `local-project-dashboard/`. See C-176 below for the
    reasoning and the ruling it rests on.
  - C-105, minor: PR 9 listed `local-health-check/` in Touches with no work
    behind it. Removed.
  - C-106, minor: "the two shadowed issues live one in the pack and one in
    the shared repository" is an unsourced specific. `prd.md:395-402` locates
    one and not the other. Addressed.
  - C-107, minor: PR 5 called Dependencies a section. The `prd.md` has five
    sections and puts Dependencies under System (`prd.md:598`). Addressed.
  - C-108, minor, recorded: the `stage`/manifest check sits in PR 3, whose repository
    does not hold `writing-pack/sd-plugin.json`. The manifest exists on
    disk, so the test can read it; the dependency is now hand-off 9.
- **2026-09-05** — Adversarial re-review, round three. Fourteen findings: six
  blocking, six material, two minor. **Every blocking finding is a round-two
  fix that failed**, and all six failed the same way: the fix was written
  where the finding pointed and nothing under it was re-derived. Round two's
  own diagnosis of round one was that exact sentence, so it is now recorded
  three rounds running.
  - C-109, blocking: round two cited hand-offs 7, 8 and 9 in three places and
    never added them. The list ended at six and said "there are six".
    Addressed: all three written, and the count is nine.
  - C-110, blocking: hand-off 7 sends criterion 23's pack half to item A's
    slice 2, and A's plan schedules none of it — no pack test file in any of
    A's Touches lists, and `sd_db.testing` nowhere in A's pages, which carry
    only the installer step. The hand-off named a destination that does not
    accept it, so the criterion closes on neither item. Addressed: the
    hand-off says what A must add, including migrating the `launchctl` and
    `tailscale` doubles.
  - C-111, blocking: hand-off 8 filed three clauses of criterion 13, and in
    the same sentence said two of them were "in no pull request body". Those
    two — the Skills promote and demote writes, and the Providers-and-bills
    registry writes — are `local-project-dashboard/` screens in this
    repository. Deferring in-repository work to a cross-item hand-off does
    not schedule it. Addressed: PR 12.
  - C-112, blocking: the closure table still gave criterion 13 wholly to
    PR 7, whose own verification says "Not criterion 13 whole". Round two
    wrote the disclaimer into the pull request and left the table.
    Addressed.
  - C-113, blocking: PR 10 claimed "Criterion 16 whole" while hand-off 5 and
    its own closure row both say the injection is item A's `SessionStart`
    hook. The pull request added to close the criterion over-claimed it on
    arrival. Addressed.
  - C-114, blocking: PR 10 and PR 11 appeared in no ordering — the list
    stopped at 9 — and both were written after "Closing the item", which
    still fired "When PR 9 merges". Addressed, and now for PR 12 as well.
  - C-115, material: three of eleven pull requests carried a **Repository:**
    line and no Touches list at all, so the collisions on
    `local-project-dashboard/`, `local-sd-db/` and `local-cron-jobs/jobs/`
    could not be read off the lists. Addressed.
  - C-116, material: criterion 17's closure row, the by-hand note and
    `design.md` all say `herdr` resumes each pane "from its row".
    `prd.md:1059-1062` says a small state file the wrapper maintains, and
    criterion 17 names no row. PR 11's own body had it right; the fix
    reached the new section and neither of the two older ones. Addressed in
    all three.
  - C-117, material: hand-off 9 recorded that `sd-plugin.json` exists and is
    readable. The real dependency is that item C's PR 3 **rewrites** it,
    extending `blog-idea`'s ladder from four targets to seven, so B's
    mapping table must carry all seven or criterion 5's tail becomes false
    the moment C's PR 3 merges. Addressed.
  - C-118, material, addressed. The `prd.md`'s
    own landing order named requirements 5, 6 and 9 for slice 4 and
    scheduled requirements 7 and 8 in **no slice at all**, while
    `prd.md:1169` says a slice claims only the criteria its text names.
    `implement.md` and `design.md` placed them; slice 4 above now names
    all five, and requirements 7 and 8 have both shipped.
  - C-119, material: `design.md`'s landing order described the nine-pull-request
    shape and lost requirement 7's notes and requirement 8's wrapper, the
    same two the section's own closing sentence warns about. Addressed.
  - C-120, material: PR 1 asserts `sd_db.testing` is importable, and `system`
    has no Python test suite. Addressed: the assertion is from the library's
    own package tests.
  - C-121, minor: "six rows close on something other than a test" counted
    clauses as rows. Three rows carry five non-test records. Addressed.
  - C-122, minor: two of round two's twenty-five re-resolved citations still
    miss. `prd.md:1324-1326` starts one line after the clause it quotes,
    which opens at 1312; `prd.md:854-868` covers the filter and the
    selection but not paging, which is at `prd.md:887-889`. The offset table
    moved the ranges and the targets were not read. Both corrected; the
    other thirty-nine resolve.
- **2026-09-05** — Correction landed from item D's round-three review.
  - C-123: this file had a `control`-scope kill "mark the row `blocked` with
    a `killed by operator` note". Item D's `prd.md:746-753` forbids a
    terminal status written by the kill, "because a row written terminal by
    the kill would be one the start never reconciles, its lease held and its
    dirty work archived by nothing", and D's criterion 4 asserts `ending`
    first and `blocked` only after the runner's next start. D's reading is
    the correct one and this file's was not. Corrected: kill writes `ending`
    with the outcome recorded and leaves the terminal status to the runner's
    ordinary end run. The mismatch had stood in both files unread since each
    was written, and D's dependency paragraph recorded agreement.
- **2026-09-05** — Round-four adversarial re-review of `implement.md`, four
  blocking findings and five material. Every one is a round-three fix that
  landed on the sentence its finding quoted and left the sentence under it.
  - C-124, blocking: C-123 corrected requirement 5's prose and the ledger and
    left acceptance criterion 12 asserting that a `control`-scope kill leaves
    the row `blocked` with the runner stopped — the state the corrected
    design forbids. Item D's round-four reviewer found it independently.
    Corrected: criterion 12 now asserts `ending` with the runner stopped, the
    row staying `ending` while it is stopped, and the terminal `blocked`
    written by the runner's first tick after it starts. C-123 touched two of
    the three sites a test is written from and the third was the criterion.
  - C-125, blocking: C-123's own commit inserted four net lines at
    `prd.md:802`, so thirty-three citations in `implement.md` and one in
    `design.md` pointed four lines high — C-100's mechanism, reintroduced by
    the round that closed C-100's residue in C-122. Re-anchored, and every
    citation past the insert was then opened rather than inferred. C-124's
    own rewrite added six more lines and fourteen citations were re-anchored
    a second time for it. Two targets did not resolve by arithmetic and were
    corrected by reading: the `start`-entry cap refusal is at
    `prd.md:1022-1024`, not `986-988` plus four — a twenty-five-line
    C-100 survivor that C-122 declared resolved — and `local-health-check` as
    existing furniture is at `prd.md:1072-1077`.
  - C-126, material: C-122 asserted as a re-derived fact that criterion 12's
    no-`package.json` clause "opens on 1312". It opens with "No build step:"
    on `prd.md:1327`. Two drafts anchored it by arithmetic and the second
    wrote the false anchor down. Corrected, and the anchor is now the clause's
    opening words rather than a bare line number.
  - C-127, blocking: C-110 found hand-off 7 naming a destination that
    schedules none of the work and fixed hand-off 7 alone. Hand-offs 4, 6 and
    8 have the same defect: `git grep -n "skill-review\|dependabot\|enqueue"`
    across item D returns nothing, so criterion 19's runner half and
    criterion 21's cutover commit close on no item, and item A's PR 6 reads
    `status_source` for item status rather than resolving `provider` and
    `bill` from rows, so criterion 13's `sd-review` clause closes on neither.
    All three now carry the note hand-off 7 carries.
  - C-128, blocking and open for the operator: no pull request in this item
    builds an `sd` verb. `implement.md`'s opening says the pack gets the
    verbs; the C-90/C-101 reversal made every pull request here `system`'s.
    Six criteria — 4, 12, 15, 16, 19, 21 — assert against `today`,
    `exec-log`, `usage`, `note`, `skill review` and `deps`, none of which
    exist in the pack (`add_parser` in `bin/sd` returns `plugin`, `store`,
    `config`, `sweep` and nothing else). Either this item needs pack pull
    requests, partly undoing C-90, or the verbs are `system`'s and criterion
    1's pack-virtualenv import has no pack caller. Recorded at the top of
    `implement.md`, not decided. PR 3's `sd shadow sync` additionally
    collides with item A's PR 8, which says of the same new command "it is
    built here"; both halves close together.
  - C-129, material, addressed: C-112 rewrote criterion 13's closure row clause by
    clause from PR 7's and PR 12's Verification lines rather than from the
    criterion, and dropped `protect main` and its owner-gated refusal, which
    then closed on nothing. Added to PR 7's Verification and to the row.
  - C-130, material: criterion 19 was assigned to PR 7, whose body and
    Verification never name a skill, a review, a proposal or a reviewer, and
    the Skills screen is read-only until PR 12. This is C-92's mechanism,
    which C-113/C-114 fixed for criteria 16 and 17 and did not re-run for 19.
    Reassigned to PR 12 with the clauses named; the `sd skill review` verb is
    C-128's and the runner half is hand-off 4's.
  - C-131, material, addressed: criterion 1's six backup-failure paths — three backup
    ages, the truncated copy, the unopenable database and the full volume,
    each asserting one email through the cron mail path — were claimed by no
    Verification and by no closure cell, and criterion 14's cron-mail
    exception rests on them by name (`prd.md:1476-1478`). Added to PR 2 and
    to the closure row. C-103 did this pass for criterion 22 and not for
    criterion 1, the other criterion split across four pull requests.
  - C-132, material: C-116 said "addressed in all three" and reached two.
    `design.md` section 8 still had each pane resuming "from the row that
    names it"; requirement 8 specifies a state file and criterion 17 names no
    row. Corrected in `design.md`.
  - C-133, minor: PR 9 quoted criterion 8 as naming three enumerations. It
    names two; `personal.cron` is work this pull request adds beyond the
    criterion, and the paragraph above it argues for it well. Restated so the
    criterion is quoted correctly and the addition is visible as an addition.
  - C-134, minor, corrected: "Closing the item" bound closure to all nine hand-offs.
    Hand-off 1 says in its own text that nothing in B is blocked if A's slice
    slips, and hand-off 2 is item C's housekeeping. Narrowed to hand-offs 3
    through 9.
  - C-135, recorded and not corrected: the citations inside this Log drifted
    with C-123's insert too — C-118's `prd.md:1108-1114` and C-116's
    `prd.md:1059-1062` among them. Ledger entries are dated records of what
    was found when it was found, so they are left as written rather than
    rewritten to match the file they describe.
  - C-136, the round's own lesson: this is the third round in which an offset
    table was applied to citations without opening the targets, and the
    second in which the round that corrected citations was the round that
    invalidated them. Any future edit that changes this file's line count
    must re-anchor `implement.md` and `design.md` in the same commit, and the
    re-anchoring is not done until each target has been read.
- **2026-09-05** — Decision, on the operator's word, closing C-128: **the `sd`
  verbs are the pack's**, and seven of this item's twelve pull requests are
  pairs.
  - The evidence that settled it: `~/repos/system` has no `bin/` and
    no `sd` binary, so `system` cannot hold a verb; the pack's `bin/sd`
    already dispatches four groups; and items C and D both wrote their plans
    against pack verbs, leaving this item the only page that said otherwise.
  - **C-90 is narrowed, not undone.** Its holding — the library is `system`'s
    and the installer step is item A's — stands. What is corrected is the
    sentence its fix over-reached into, "every pull request in this item is
    `system`'s", which was true of the library and false of the CLI.
  - The pairs, and the verb group each adds: PR 2 (`restore`), PR 4 (`today`),
    PR 5 (`exec-log`), PR 6 (`deps`), PR 8 (`usage`), PR 10 (`note`), PR 12
    (`skill`). **`system` lands first in every one**, because here the pack
    verb is the caller and `sd_db` is what it calls. Item D's pairs land the
    other way round, because there the runner is the caller.
  - `sd shadow sync` is item A's PR 8, which already claimed it as new work.
    PR 3 keeps the `watermark` state kind and the cron entry that invokes it,
    and the dependency is recorded as hand-off 10 — the one hand-off of the
    ten whose destination already accepts it.
  - Item C's PR 7 builds the piece-specific half of `sd restore reimport`
    against PR 2's verb group.
- **2026-09-05** — `sd-docs-lint` gains rule 6, which watches this file's line
  numbers. Six findings across four review rounds were citation drift, and
  twice the round that corrected the citations was the round that invalidated
  them. Each citation in this item is now recorded in `.citations.tsv` against
  a snippet of the line it was written for; when the snippet moves, the rule
  says where it went. Re-anchoring is a read, not arithmetic. The baseline
  records what the pages say today and does not certify that any citation was
  right when it was recorded — C-136's rule still applies, and the manifest's
  own diff is the artifact to review after any edit.
- **2026-09-05** — Round five, targeted at criteria 7 and 15 alone. The
  reviewer enumerated criterion 7 into twenty-one clauses and criterion 15
  into twenty-eight, from `prd.md` first, and then asked of each which pull
  request could reach it. Six of criterion 7's twenty-one were inside the
  credited pull request's reach.
  - C-137, blocking: no pull request's Verification in this item named
    criterion 7. `grep -i "criterion 7" implement.md` returned one line, the
    closure note, and not one pull request body. Round four found this exact
    shape for other criteria and it was not re-derived here. Corrected: the
    closure row now splits twenty-one clauses across PR 2's pack half, PR 3,
    PR 4, PR 7 and two of item A's, and PR 3, PR 4 and PR 7 each gained the
    Verification lines for their share.
  - C-138, blocking, addressed: clause 7.2 requires `sd-docs-lint` to fail **on** a
    `status:` line; rule 1 fails on its **absence**
    (`bin/sd-docs-lint:121-123`). No pull request in either item scheduled
    the inversion, and PR 3, which the closure row credited, is `system`-only
    and cannot reach a pack file. Scheduled in item A's PR 7, whose Touches
    already name the file and whose commit removes the lines.
  - C-139, blocking: criterion 7 asserts refusals from `sd status` and
    `sd assign cancel`. `bin/sd` carries `plugin`, `store`, `config` and
    `sweep`, and the seven scheduled pack halves add `restore`, `today`,
    `exec-log`, `deps`, `usage`, `note` and `skill`. Neither verb existed
    anywhere. PR 7 gains a pack half and the pairing count rises from seven
    to eight. Round four settled this for six criteria; criterion 7 was not
    in that pass. [Superseded 2026-09-16, owner decision (a): PR 7's pack half is the existing `sd task status <item> <status>`, `sd run --sequential|--parallel` and `sd assignments cancel` / `sd runner cancel`; no `status` or `assign` group is built, and the three clauses stay open until their fixture tests land.]
  - C-140, blocking, addressed: clause 15.1 needs `local-agent-meter/agent-meter.py` to
    write a `meter` row on its four-hourly schedule. The file is in no
    Touches list in the item, and PR 9, which owns `local-cron-jobs/jobs/`,
    enumerates the jobs it adds as exactly two. Added to PR 8's Touches with
    the dual write `prd.md:908-910` still requires.
  - C-141, blocking: PR 8 claimed "Criterion 15 whole" while clause 15.15
    needs the dashboard's cap control, which is PR 12's and lands after PR 8.
    Corrected: the clause moves to PR 12's Verification and PR 8's line names
    its three exclusions. A whole-criterion claim is now the thing to check
    against the ordering, not a summary of intent.
  - C-142, material, corrected: the closure note generalised one clause's dependency to
    all twenty-one and said "the test" singular. Four clauses depend on
    nothing outside this item; five depend on PR 2's pack half, not PR 3;
    four depend on item A for reasons other than the retire. Rewritten as the
    enumeration.
  - C-143, material, addressed: clause 15.6 charges a review pass through
    `bin/sd-review`, which calls provider CLIs by subprocess and is in no
    Touches list here. Added to PR 8's pack half. Its companion grep, clause
    15.7, would have passed at PR 8 against a `local-sd-runner/` that does
    not exist; the runner half moved to item D's PR 7, where there is a
    runner to grep.
  - C-144, material: PR 8's Touches called `sd usage` a printer.
    `prd.md:993-996` and `prd.md:1015-1016` make it a mutator — it releases
    orphaned reservations and settles dead-owner `sending` rows — and clauses
    15.19 and 15.20 assert the ledger changed across a run of it. Restated,
    with the sweep in the `system` half and the verb calling it.
  - C-145, material, addressed: clause 15.25's `shipped_at` spans three writers PR 8
    reaches none of. `shipped_at` and `Closes:` appeared in **no** file of
    item A. Split: the arithmetic is PR 8's, the import's write is PR 3's,
    and the merge write is scheduled in A's PR 7.
  - C-146, material, corrected: hand-off 5 still gave PR 8 criterion 16's rows and
    brief. PR 8's own Verification disclaims them, PR 10's body carries them
    and closure row 16 names PR 10. The correction had landed in three places
    and not in the list the reader is pointed at. This is C-136's shape
    inverted: not a claim written once and stale in three, but a fix landed
    in three and missing from the one.
  - C-147, minor, addressed: clause 15.26's detail view was decided inside PR 4's body
    and verified by neither PR 4 nor PR 8. Added to PR 8's body and to PR 4's
    Verification, which also gained clause 7.16.
  - C-148, minor: open question 1 commits to a nightly job writing the
    `github` bill's `meter` rows; PR 9 enumerates two jobs and this is a
    third. Recorded as hand-off 14 with the writer in PR 8 and the cron entry
    in PR 9, PR 9's own enumeration left as the authority on the count.
  - Open question 8 opened: **"the usage read"** carries three clauses and
    names no mechanism. It needs an operator answer before PR 8 is written.
  - C-149, decision, resolved: **open question 8 settled by the operator on the same
    day it was opened.** "The usage read" is the total a `start` session
    reports at its own exit, read and written by the thing that started it,
    one `run` row per session carrying the assignment and the pass. The
    three readings the finding offered were all rejected, and for one
    reason: the four-hourly `local-agent-meter` sampler and a vendor billing
    endpoint are both provider-wide with no assignment, and `sd usage` may
    never be run at all, so none can satisfy clause 15.13's `assignment` and
    `pass`. Per-call rows were rejected with them — the registry reader
    refuses a `start` entry on a capped bill, so there is no cap to itemise
    for, and cost per shipped item, the four weekly numbers and the usage
    report's grouping all read a sum. Clause 15.13 changes from "three `run`
    rows" to one row carrying the session's reported total; 15.16 names the
    exit read and the runner. The writer moves to **item D's PR 7**,
    hand-off 15, which already writes the `exec` row's cost from this
    item's library function; PR 8 keeps the schema, the `url` path and the
    sweep. Round thirty-three's session bound, withdrawn at round
    thirty-four, is what left the phrase standing with no mechanism under
    it for eight rounds.
  - Verified sound and recorded as such: PR 8's five `prd.md` anchors all
    land on the substance they claim; `implement.md:568-570`'s pairing list
    agrees with PR 8's "`system` lands first" and with closure row 15; and
    the on-disk presence of `local-project-dashboard/` and
    `local-agent-meter/` against the absence of `local-sd-db/` and
    `local-herdr/` is stated correctly throughout.

- **2026-09-05** — Executable review, round fifty-five. A reviewer built the
  fixtures and ran the commands these pages prescribe against the real tree
  instead of reading them. Five blocking findings, every one in a command four
  prose rounds had read and none had executed.
  - C-150, blocking: PR 2 claimed criterion 2 whole. `git grep -n
    sqlite3.connect` over the pack returns `dashboard/store.py:89` [quoted: grep output], so at PR
    2's merge there are two callers and the second is in no Touches list here.
    The criterion closes at PR 6, which retires the `dashboard` package. The
    sentence "there is exactly one caller to keep honest" described a tree
    with the retirement already done. Addressed: PR 2 establishes, PR 6
    closes, and the closure row says so.
  - C-151, blocking: the retire step switches off rule 2. `check_ready` reads
    the same frontmatter and returns early on `if status not in
    WORKABLE_STATUSES: continue` (`bin/sd-docs-lint:141-143`), so removing
    every `status:` line stops three checks — acceptance criteria stated, no
    open `BLOCKING:` line, `in_progress` records its branch — for every active
    item in every registered repository. A fixture with the line fails all
    three; without it, `rule 2 failures: []`. Criterion 7 asserted only rule
    1's sign, so the item could pass every clause it wrote and still land a
    linter that stopped checking the two things it was mainly for. `rule 2`,
    `check_ready` and `WORKABLE_STATUSES` appeared in neither this item's
    pages nor A's. Addressed: criterion 7 gains the clause, and `check_ready`
    joins A's PR 7 Touches.
  - C-152, blocking: PR 6 retires the pack's `dashboard` package and its
    Touches reached no importer of it.
    `git grep -ln 'from dashboard|import dashboard' -- bin tests` returns
    sixteen files — `bin/sd-dashboard`, `bin/sd-status`, `bin/sd-trackers`,
    `bin/sd` and twelve test modules. All sixteen break at that merge.
    `bin/sd-trackers` is the one with no successor anywhere in these pages:
    `dashboard/jira.py` is Jira collection that `shadow` does not replace.
    Addressed: the sixteen are Touches, and the Jira path is called out as an
    open decision rather than assumed.
  - C-153, blocking: PR 6 deletes the binary a live launch agent runs.
    `local.system-tools.sd-dashboard.plist` execs
    `sd-ai-command-pack/bin/sd-dashboard serve --port 8767`, and PR 9 keeps
    the label in `personal.agent` on purpose, so after PR 6 launchd starts a
    removed file at every login and nothing serves `local-project-dashboard/`.
    Criterion 12's front door had no process behind it. Addressed: the plist
    joins PR 9's Touches and is repointed at the new server, keeping the label
    so `machine-setup.sh status` still reconciles it. Same class as the
    `com.platypeeps.sdw-meter` lesson PR 9 already cites.
  - C-154, blocking: a `meter` cost row cannot be populated. Over 167 readings
    the sampler's ledger mentions `usd`, `costUsd` and `tokens` **zero**
    times; what it holds is `windowMinutes` and `usedPercent` per provider for
    two windows. The `cost` row declared `usd`, tokens and no percentage, and
    criterion 12's gauge clause needed one row to carry two different
    percentages. Addressed: the row gains `window_minutes` and `used_percent`,
    a `meter` row is written per provider **per window** so two gauges read
    two rows, and `usd` and tokens are null on it — which the four weekly
    numbers already assumed by excluding `meter` rows.
  - C-155, material, corrected: hand-off 8 cited `prd.md:1474-1476` for criterion 13's
    registry clause. That range is criterion 14's opening, about report email
    retirement. The clause is `prd.md:1469-1472`.
  - C-156, material, corrected: hand-off 15 cited `D/prd.md:498` for the `start`-session
    decision. That line is about requeuing a row on a fresh worktree; the
    phrase quoted is at `D/prd.md:524`.
  - C-157, minor: this page and A's gave two ranges for the same three lines
    of rule 1. `bin/sd-docs-lint:121-123` is right — `:121` reads the status,
    `:122` tests it, `:123` fails. A's `122-124` dropped the read and added an
    unrelated line; corrected there.
  - C-158, material: the fix sweep for C-150 through C-157 mis-shifted one
    citation and the first verification missed it. A citation-shifting script
    was given an "already applied" escape — skip a rewrite whose old value is
    absent and whose new value is present — which fired on a value another
    edit had just written, and moved clause 15.7's runner-grep citation from
    the grep to an unrelated cap clause. The first check compared the citation
    record's snippets as a **set** and reported one lost and one gained, both
    explainable; the wrong target's snippet already appeared in the record
    under two other citations, so the set hid it. Compared as a **multiset**
    the same data shows `1 -> 0` and `2 -> 3`, which is the defect. Corrected
    to `prd.md:1526-1527`, and both this item's and A's records re-verified
    against their pre-session baselines with counts: every other citation
    anchors to the same text it did before.
  - C-159, material: C-152's Jira half was wrong about the consequence.
    `dashboard/jira.py` is wired into `TRACKERS` but has never collected:
    the index holds `github|1175` and no Jira row, `tracker_watermark` holds
    only GitHub's line, and two of the three variables `jira.settings` needs
    are unset in both the shell and the dashboard agent's plist. It retires
    with the package and nothing is lost. The real obligation is
    `bin/sd-trackers`, which is not a collector at all — it resolves
    `sd-plan --from gh:o/r#N|jira:KEY` into a reference block, is named by
    `skills/sd-plan/SKILL.md`, and is run by no job or agent. Its GitHub half
    works today and must survive PR 6; its Jira half already exits 2 as
    unconfigured. Recorded because the first finding named the right file for
    the wrong reason, and a reader acting on it would have preserved a dead
    collector and still broken a live seeding path.
  - C-160, blocking, found on starting PR 1: PR 1's Touches was
    `local-sd-db/sd_db/testing/` alone and could not satisfy PR 1's own
    Verification. Measured 2026-09-06: `local-sd-db/` does not exist,
    `git ls-files '*.py'` in this repository returns five loose scripts and no
    package, no `pyproject.toml` is tracked anywhere, and `CLAUDE.md:4` says
    "Not a product codebase: no build, no test suite, no CI." A `testing`
    subpackage with no parent, no packaging metadata and no runner does not
    import, so "the package imports" had nothing to import and hand-off 7's
    "PR 1 asserts the import from the library's own package tests" named tests
    no pull request created. PR 1's Touches is now `local-sd-db/` whole plus
    `CLAUDE.md`, which this folder is the first thing here to contradict on
    three counts. Recorded rather than absorbed: the Touches list was derived
    from the criterion's wording and never from the state of the tree it
    lands in, which is the same shape as A's C-174.
- **2026-09-06** — C-161, from building PR 1. The Touches paragraph said
  `local-sd-db/` would break three of `CLAUDE.md`'s conventions. Two of the
  three were avoidable and were avoided: the folder ships `sd-db`, a POSIX sh
  entrypoint with subcommands, so conventions 1 and 2 hold. Only `:4` "no
  build, no test suite, no CI" gives. The paragraph is corrected, and
  `CLAUDE.md`'s amendment is narrowed to match: one exception, not three.
  Recorded because the finding is that a plan predicted a cost the work did
  not have to pay, not that the work diverged.
- **2026-09-06** — C-162, from building PR 1. The fixture provider and the
  `local-notify` stub decided whether to read stdin with `sys.stdin.isatty()`.
  A process spawned with an inherited pipe is not a terminal, so both read,
  and both blocked waiting for a writer that never closed — the suite hung for
  nine minutes before it was killed. Both now read only a regular file or a
  pipe, checked with `os.fstat(0).st_mode`, and the tests spawn with `input=""`
  rather than an inherited stdin. Two layers, because either alone leaves the
  other caller exposed.
- **2026-09-06** — C-163, from building PR 2. The entrypoint was named `sd-db`,
  not `sd-db.sh`. Convention 1's own example is `local-redis/redis.sh`, and
  this document's criterion 1 says `local-sd-db/sd-db.sh migrate` in the
  refusal text a user is meant to type. The script is renamed with `git mv`,
  and C-161's sentence "the folder ships `sd-db`" reads `sd-db.sh` from here
  on. The same convention took a second script with it: `sd-db-backup` was a
  separate runnable beside the entrypoint, which convention 1 forbids in the
  same breath as it forbids `run-X.sh`. The backup is now `sd-db.sh backup`;
  the scheduled job keeps the label `sd-db-backup` because a launchd label is
  not a script. Recorded because both were conventions the plan claimed to
  hold, and holding them was checked against the convention's wording rather
  than against its example.
- **2026-09-06** — C-164, from building PR 2, a declared deviation. Criterion 1
  lists "fills the backups volume" among the backup failures that must send
  one mail and write nothing. The test meets that clause with a destination
  that cannot be written, not with a full volume. A literal full volume needs
  a mount this suite would have to create and detach, and a mount that
  outlives a failed run is worse than the gap it closes. What the code
  distinguishes is "the snapshot was not written", which both conditions
  produce identically, and the distinction the criterion cares about — one
  mail through the cron path, no row in the store — is exercised. Stated in
  the test's own docstring as well as here, so a reader who finds the test
  before the ledger still finds the reason.
- **2026-09-06** — C-165, from building PR 3. Criterion 7 says the `docs/work`
  migration reads items "outside the archive", and the first reader took that
  as a description rather than as a filter. Enumerating every `prd.md` under
  `docs/work` across the fleet returned 1,258 items in eleven repositories, of
  which 486 were the pack's `sd-ai-command-pack/docs/work/archive/` alone. Matching
  `docs/work/<slug>/prd.md` exactly — four path segments, no deeper — returns
  **64 items across six repositories**, which is the number this document
  already carried. Recorded because the criterion's own count was the thing
  that caught the reader: a migration whose result cannot be checked against a
  written number would have imported the archive silently and looked fine.
- **2026-09-06** — C-166, from building PR 3. Criterion 7 says the migration
  reads every branch of the remote and refuses when branches disagree about an
  item's status. On real data that refusal fired three times, and two of the
  three were not disagreements. A branch can carry an older copy of a file that
  the default branch has since superseded; reading it as a competing claim
  makes every stale branch a permanent blocker. The rule added: a candidate
  whose last commit **touching that file** is already an ancestor of the
  default branch is history, not a claim, and the default's own candidate is
  never superseded. Note the granularity — a whole-branch merged check was
  tried first and was not enough, because a branch can be unmerged overall
  while its copy of one file is not. After the filter, two genuine
  disagreements remain, both in this pack, both awaiting a merge or a branch
  deletion rather than a code change.
- **2026-09-06** — C-167, from building PR 3. The register migration found O27
  and reported O28 and O29 missing. The entry regex was line-anchored; the
  register hard-wraps at eighty columns, and two of its three open entries
  carry a bold heading that spans two lines. `re.DOTALL` fixes it. What is
  worth recording is not the regex but why the gap surfaced at all: the
  migration refuses when the header's open range names an entry the body does
  not yield, so the file's own summary line audited the parser. A reader that
  imported whatever it happened to match would have landed one row and
  reported success.
- **2026-09-06** — C-168, from building PR 3. This document counts two open
  GitHub issues under `author:@me is:issue is:open`. The rehearsal answers
  **three**, one in a repository the operator does not own,
  `mProjectsCode/obsidian-meta-bind-plugin` and `mindfold-ai/Trellis`. Nothing
  in the design depends on the number, and the count was a fact about a moment
  rather than a requirement, so no criterion changes. Recorded because a
  written count that has drifted is indistinguishable from a collector that
  missed one, and the next reader deserves to know which this was. The scope
  bullet is rewritten to name no count, because a number in a live scope line
  is a fact that goes stale on its own; the two decision records that also say
  "two" keep the word, since they record what was said on the day and
  correcting them would falsify the record. The same bullet said the issues
  were "in the operator's repositories", which was wrong in a way the count
  hid: the query is `author:@me`, and all three are issues the operator filed
  in repositories owned by other people.
- **2026-09-06** — C-169, from building PR 3, a gap in this document. Criterion
  6 binds the enumeration to the `repo` table rather than to the disk, and the
  plan never said how that table fills. A migration whose bound is an empty
  table passes every check it has while reading nothing, which is the failure
  mode a criterion phrased as a bound cannot catch on its own. PR 3 adds
  `sd-db.sh repo add`, which registers a checkout after refusing anything that
  is not one or that sits inside the worktrees directory, and a one-shot
  `sd-db.sh repo seed` reading `local-repo-sync/repos.personal.conf` — the file
  that already lists the personal fleet, so seeding restates no fact. The
  `docs/work` migration refuses when a registered path is not a checkout, so
  the table going stale is a refusal rather than a silent shortfall.
- **2026-09-06** — C-170, from building PR 3, an open defect in what PR 3
  lands. Criterion 7 says the `docs/work` migration reads every branch of the
  remote. It reads `refs/remotes/origin/**`, which is a cache of the remote and
  not the remote. The difference is not theoretical: on 2026-09-06 the pack's
  checkout held eleven of them where `git ls-remote` answered four, and the
  seven extra had been deleted on the server. Two of those ghosts produced the
  cross-branch disagreements recorded in C-166, which is to say the refusal was
  correct about its inputs and wrong about the world, and a person acted on it
  before noticing — the branches were reported to the operator as needing a
  merge or a deletion when they had been deleted already. The failure runs the
  other way too and is worse: a branch pushed since the last fetch is invisible,
  so an item living only on that branch is silently absent rather than refused,
  and absence is the one outcome this design has no check for.
  The fix is **not** a fetch. A migration that mutates the checkout it reads
  stops being a reader, needs the network to run at all, and would rewrite the
  refs whose disagreement is the evidence. The shape that fits: compare
  `git ls-remote --heads origin` with the checkout's remote-tracking refs at
  freeze time and refuse when they differ, naming each stale or missing ref and
  the `git fetch --prune` that settles it — the same move the migration already
  makes for a dirty working tree, which it refuses on rather than stashing.
  A checkout with no network reaches the refusal rather than a wrong answer.
  Deferred rather than fixed here because it changes what criterion 7
  asserts. Fixed: `fresh_origin` in the migration's own reader, #227.
- **2026-09-07** — C-176, from building PR 4. **Decided by the operator the
  same day: resolution (a), the criterion narrows.** Criterion 12 said "the
  repository contains no `package.json`, `node_modules`, or bundler config"
  (`prd.md:1359-1362`; the clause opens with "No build step:" on 1343). The
  clause is false on disk. Checked in this repository on 2026-09-07 rather
  than taken from the plan: `git ls-files | grep package.json` returns exactly
  one line, `mezmo-webhook/package.json`, tracked on `main` right now. So the
  criterion cannot pass as written, and no pull request has addressed it.
  Two resolutions, and the choice is the operator's:
  **(a)** narrow "the repository" to `local-project-dashboard/` in this
  `prd.md`, which is what the clause is actually about — the sentence
  continues "and the dashboard serves one CSS file and one JavaScript file
  from disk", so the subject of the whole clause is the dashboard, and
  `mezmo-webhook/` is an unrelated Node service that has nothing to do with
  the dashboard having no build step; or **(b)** schedule `mezmo-webhook/`'s
  removal, which makes the clause true as written but attaches a retirement
  to a criterion that was never about that folder.
  PR 4's test asserts the narrow half either way:
  `git ls-files -- local-project-dashboard` returns no `package.json`, no
  `node_modules/` path and no bundler config, and the dashboard serves exactly
  `dashboard.css` and `dashboard.js`. What it does **not** assert is the
  repository-wide reading, because that reading is currently false and a test
  written to pass over it would be the criterion quietly rewritten by the
  pull request that could not meet it.

  **The ruling, and it is broader than this clause.** The operator's words:
  *"whatever does not reduce existing functionality. We gotta get out of the
  proposal to remove functionality to maintain a cap. I will never agree to
  that. If functionality requires more code, then it requires more code."*
  Resolution (b) would have deleted a working Node service to make a sentence
  about the dashboard's build step true. That is the shape the ruling forbids.
  So the criterion's subject becomes `local-project-dashboard/`, which is what
  the clause was always about. `mezmo-webhook/` is untouched and stays in the
  repository. The test PR 4 already wrote is now the whole of the clause rather
  than its narrow half. The clause is true on disk as amended:
  `git ls-files -- local-project-dashboard` returns no `package.json`, no
  `node_modules/` path and no bundler config.
- **2026-09-08** — C-177, from retiring the `skill-proposal` kind. The kind
  leaves the writing pack's manifest in item A, and the four
  `("skill-proposal", ...)` rows leave `sd_db/sources/vault.py` here. The two
  cuts land in different repositories, and they are **ordered** — the lane that
  made the second one was briefed that they were not. Criterion 5's assertion
  runs manifest -> table: a row this table holds that the manifest does not
  declare is not a failure, which is exactly what lets the table carry
  `blog-idea`'s seven targets ahead of item C. The reverse direction is a
  failure, and this cut is the reverse direction. Measured on 2026-09-08 with
  the rows removed and the manifest untouched:
  `TheMappingTableCoversTheManifest` fails naming `skill-proposal/accepted`,
  `/declined`, `/filed` and `/proposed`, one failure in 321 tests. Against a
  manifest with the kind removed, the same 321 pass. So the manifest cut merges
  first and this repository's cut merges after it, and a merge lane that takes
  them in the other order breaks the suite on `main`. Recorded because the safe
  direction and the unsafe one are the same assertion under the same name, and
  nothing in that name says which way it reads.
- **2026-09-12** — PR 10's `system` half landed as #277: the eight-kilobyte
  brief builder, `sd_db.brief.note_brief`, with `reads.brief_items` and
  `reads.brief_notes` under it and `local-sd-db/tests/test_brief.py` binding
  criterion 16's library clauses. The note write, `resolve_note`, Today's
  open list and the item screen's resolve route were found already on `main`
  from #227, which is why this pull request is one module and one test file
  and not the whole requirement. The dashboard's resolve button now names
  `sd note resolve <id>`; it said `sd task resolve`, a verb that does not
  exist. `sd note list <item>` and the hook's call are the pack's and item
  A's, and remain open.
- **2026-09-12** — PR 9's remainder, `feat/backbone-doctor`: the tracked
  `local.system-tools.sd-dashboard.plist` now execs
  `local-project-dashboard/dashboard.sh serve --port 8767` — the installed
  copy already did, and `machine-setup.sh status` had reported the pair as
  `DIFFERS` since the dashboard's installer replaced it; the bytes are the
  installer's, so the next install reads as `ok` too. `machine-setup.sh`
  gained an `sd` stage (the database init and the `tailscale serve` route,
  each a `MISSING` line `status` counts), the four `doctor` checks of
  requirement 9 with the installed-`sd_db` version report beside them, a
  `doctor sd` scope, and a unittest suite under `local-machine-setup/tests/`
  wired into `system-native` — criterion 22's profile, dry-run, drift, doctor
  and `backup-verify.conf` assertions. `personal.agent` already named
  `local.system-tools.sd-runner` and `personal.cron` already listed `sd-db-backup` and
  `shadow-sync-nightly`, both from #227. Still open from this section: the
  six vault jobs (the operator's call — hand-off 2 keeps the vault the writer
  for vault kinds until item C), the retention table and nightly row prune
  (`sd-db-backup` runs `backup --keep all` by the September 9 amendment, and
  no row prune exists), the unset-variable check on enabled provider
  entries, and the tracked `local.system-tools.sd-runner.plist`, which `status` still
  reports `DIFFERS` and item D's PR 8 owns.
- **2026-09-12** — PR 11 landed as #279: `local-herdr/`, requirement 8,
  read against `herdr 0.9.0`. That version already reports each pane's
  agent session id through its installed integrations and resumes
  supported agent panes itself after a server restart
  (`resume_agents_on_restore`, default on), so the wrapper is the named
  session `sd`, a state file of `pane`/`agent`/`session_id`/`cwd` entries
  snapshotted from `herdr agent list`, and a `resume` for the panes herdr's
  own restore cannot bring back. Requirement 8's other branch — drop the
  wrapper, ask upstream — does not apply; the state file's shape is in the
  folder's README in case it ever does. Criterion 17 remains by hand: the
  steps are in the README, and the record of running them, with the
  version, is still owed here.
- **2026-09-12** — PR 9's row prune, `feat/sd-db-retention-prune`:
  `sd_db.retention` runs inside `sd-db.sh backup` after the snapshot passed
  and refuses otherwise, verifying the dated directory again rather than
  taking the caller's word. `exec` output at ninety days with the note kept
  and marked `output_expired` (C-66), `heartbeat` rows to one per key, `cost`
  rows never (C-70), one `report` item with the counts. Two gaps recorded:
  the request log has no store to prune -- the dashboard writes none -- and
  the `budget spent` refusal criterion 22 calls after the prune is
  requirement 6's reservation, not yet built, so the test holds the ledger
  it will read. `--keep all` stays, per the amendment below.
- **2026-09-12** — Operator decision on PR 9's last open piece, the six
  vault jobs: the middle path. `vault-cleanup` and `vault-map` are
  maintenance, not process, and stay under this item for good; the four
  process jobs stay loaded until item C (sd:232) lands what replaces them,
  and their removal is item C's, asserted there. Requirement 3 amended,
  criterion 8 narrowed to four and moved. What PR 9 still owes this item
  is the `doctor` check for an enabled entry whose variable is unset;
  the tracked `local.system-tools.sd-runner.plist` is item D's PR 8.
- **2026-09-12** — PR 9's last clause, `feat/doctor-names-unset-provider-keys`:
  `machine-setup.sh doctor` reads the merged provider registry through the
  runner's interpreter, sources `~/.config/shell/env.sh` in a subshell the
  way `runner.sh serve` does, and prints one `FAIL … MISSING` line per
  enabled entry whose `env:` variable is unset or empty there — the entry
  and the name, never a value. Criterion 22's test seeds a fixture registry
  with an enabled entry naming `FIXTURE_KEY_ONE`, an enabled entry naming
  nothing and a disabled entry naming `FIXTURE_KEY_TWO`, and greps the
  output: the first is named, the second is not, and the fixture value
  never appears. With this and the vault-jobs decision above, PR 9 has
  nothing left under this item; the runner plist is item D's PR 8. On `sol` the
  check passes for all five enabled entries, and the `provider` table is
  empty — nothing seeds it but the dashboard's `configure` — so the file's
  `enabled` is what the merge returns there.
- **2026-09-12** — Criterion 17, by hand: the operator ran the resume
  against `herdr 0.9.0` (integrations `claude` and `codex` current) and
  accepted it — "herdr criterion 17 by hand is fine, no need for more
  testing/verification". Recorded with the version, as the criterion asks;
  the decision note is on the row.

- **2026-09-12** — Retention settles clean run reports (sd:547).
  `reporting.ingest` opens every `report` row as `planning`; an attention
  report carries a followup and waits for `acknowledge`, a clean one carries
  nothing and waited for nobody — 127 sat in `planning` on the live database
  this morning. `retention.settle_clean_reports` moves a clean report older
  than seven days to `done` through the same `transition` `acknowledge`
  uses, as `retention`, so the history reads `planning -> done by
  retention`; the row and its evidence stay. An attention report or one with
  an open followup is never touched. The count rides the prune's `Pruned`,
  its report row's `removed` and the backup verb's `pruned:` line as
  `clean_reports`. The retention table above and in `local-sd-db/README.md`
  gained the row; this entry is appended, not inserted, so no citation into
  this file moved.

- **2026-09-12** — The retention table stops naming a request log (sd:541).
  Requirement 9's bullet aged "the request log thirty days"; no such store
  exists. The dashboard silences its HTTP log (`server.py`'s `log_message`)
  and writes no request rows, and the only record shaped like a request is
  the operator-action checkpoint row, which `reporting.metrics` counts by
  week — a mutation count, not a log. `sd_db.retention` had already left the
  rule out and said so in its docstring; the bullet, PR 9's list in
  `implement.md` and that docstring now agree, each reworded in place so no
  cited line moved. Criterion 12's adoption gate still reads "the request
  log shows GET requests on five of seven days" against the same log nothing
  writes; it is left as written for the operator to restate. Appended, not
  inserted, like the entry above.

- **2026-09-17** — The three criterion-7 amendments of #437 are approved by
  the owner (note 2706); each marker on this page now says so. The 7.19
  recovery is decided the same day: `sd runner cancel` ends a `running`
  row with no `runner_run`, delivered by sd:991. Appended, not inserted.

- **2026-09-23** — Criterion 12's adoption gate is restated by the operator
  (note 3730). It counted GET requests in a request log that nothing
  writes; it now counts `state` rows of kind `checkpoint` keyed
  `operator-action` on five of seven days. The dashboard writes one such row
  for each POST action that changes the database
  (`source:local-sd-db/sd_db/reporting.py::observed_action`); a visit that
  only reads writes none. `implement.md` gives the query. Criterion 1 now
  names `sd_db.schema.TABLES` in place of "the eleven tables": migrations
  004, 005, 006 and 011 added `publication_claim`, `runner_run`,
  `runner_lease`, `repo_protection` and `judgment`, so the live database
  holds sixteen. Both edits keep their line counts, so no cited line moved.

- **2026-10-04** — The 7.19 recovery decided on 2026-09-17 is delivered by
  sd:991, on branch `runner-cancel-no-run`. A `running` assignment that no
  unreleased `runner_run` owns is now ended by `sd runner cancel <id>`: both
  `source:local-sd-db/sd_db/runner.py::request_cancel` and
  `source:local-sd-db/sd_db/runner_controls.py::control` write `cancelled`
  with a `cancelled by <who>` result, and wait on no runner. An attempt that
  is still owned is only asked to stop, as before. The status refusal now
  ends "it is a running assignment without a runner run; end it with
  `sd runner cancel N`", and the item screen names the same verb but renders
  no control. `sd assignments cancel` still refuses every `running` row. The
  refusals that criterion 7's paragraph quotes for this gap are history from
  that branch on. Appended, not inserted.

## September 9, 2026 backup-retention amendment

Initial scheduled activation uses `backup --keep all` and deletes no backups.
Explicit numeric retention applies only to complete backups with matching path-bound manifests, content inventories, and SQLite checkpoints.
Legacy, moved, malformed, changed, linked, and unrelated directories remain untouched.
The 30-backup policy remains available through an explicit `--keep 30` request.

## October 3, 2026 verifier amendment (sd:1157)

Operator decision, 2026-10-03: `offsite-verify` is the sampler requirement 9 means.
It is `local-mirror-sync/offsite-verify.py`; `local-backup-verify`, which this document names elsewhere, does not exist in this repository.
Where this document says `local-backup-verify` or `backup-verify.conf`, read this section.
It differs from what requirement 1 and requirement 9 described, in three ways:

- It reads no `backup-verify.conf` and samples no files unchanged for seven days.
- It copies the newest `sd-db-backup` snapshot off the NAS share and runs the full `sd_db.backup.restore()` against a throwaway home.
- It compares the restored counts with `backup-manifest.json`, re-hashes each companion file, and fails unless the share is a network mount.

The snapshot reaches the share through the `mirrors-nas.conf` pass that `sd-db-backup` runs after a successful snapshot.
`local-mirror-sync/README.md`, section "Proving the database snapshot on the NAS", is the current description.
