# Local SD runner

The runner claims database assignments and repository leases before creating an
independent clone. `runner.sh` is its public service entrypoint; the command pack's
`sd run`, `sd runner`, and `sd worktree` commands are thin shared-library gateways.
All SQL belongs to `local-sd-db/sd_db`, including queue and completion transactions.

## Installation boundary

The service is not activated by this checkout. First provision the work volume,
install the current `sd_db` wheel in the command pack's environment, review the
configuration and `runner.sh install-plan` output, then install that exact plist.
The installed service label is `<prefix>.sd-runner`; the prefix comes from
`SYSTEM_TOOLS_LABEL_PREFIX` (default `local.system-tools`).

`~/.config/sd/runner.json` supports these explicit paths and defaults (write
absolute paths; `~` is not expanded; `/Users/you` stands for your home):

```json
{
  "database": "/Users/you/.local/share/sd/sd.db",
  "work": "/Users/you/.local/share/sd/worktrees",
  "retention": "/Users/you/Documents/sd-backups/worktrees",
  "pack": "/Users/you/repos/platypeeps/sd-ai-command-pack",
  "interval_seconds": 10,
  "free_floor_gb": 40,
  "path": ["/Users/you/.local/bin"]
}
```

`path` is optional: directories searched before the login shell's `PATH`.
launchd starts the runner with a short fixed `PATH`. At startup the runner asks
the account's login shell for its `PATH`, then searches `path`, that answer and
its own `PATH`, in that order. Every tool it starts in a clone gets this search
path: the provider session, the dependency install, `sd-check` and `sd-ship`.
The heartbeat's `executables` names each registry `start` provider's program as
the absolute path it resolved to, or null.

The accepted user-facing work and retention paths should be symlinks to
`/Volumes/sd-work/worktrees` and `/Volumes/sd-work/retained`. The preflight requires
existing directories, a work device different from the database device, retention
on the work device, APFS, and an actual positive `CapacityQuota` greater than the
free-space floor. An unbounded APFS volume fails, even if statvfs reports ample
space. Both database and work free space must be at or above the floor to dispatch.
A low database floor stops owned process groups; a low work floor pauses dispatch
while existing cleanup continues.

The free-space checks read statvfs on every pulse. The APFS and quota answer comes
from `diskutil`, which the daemon asks once per mount identity and again every
15 minutes, not on every pulse (sd:1941): a `diskutil` that stalls past its 20 s
timeout on a refresh keeps the last answer and names the failure under the
heartbeat's `storage.verification.refresh_problem`, an answer that names a problem
replaces the last one, and an answer an hour old with no successful refresh is
forgotten, so a stall that lasts becomes the problem again. The daemon's first
preflight has no answer to keep. When `diskutil` gives none (a timeout or a
non-zero exit), the daemon writes an unhealthy heartbeat with the problem and a
`starting` field, dispatches nothing, and asks again each interval for 10 minutes
before it refuses the start (sd:1950). A definitive answer, not APFS or no quota,
refuses at once. `runner.sh preflight` asks every time.

The work volume `sd-work` has a **100 GB quota** in APFS container `disk3`.
The September 8 provisioning proposal set 60 GB. On 2026-09-26 the volume was
recreated at 100 GB, because 60 GB left too little room above the 40 GB floor.
The quota is a ceiling, not a reservation; other volumes still consume the same
physical container. Source clones, builds, retained history, and preserved
outputs need continuing monitoring. Spotlight indexing is off on the volume
(`mdutil -i off /Volumes/sd-work`).

The original provisioning commands, with the current quota:

```sh
diskutil apfs addVolume disk3 APFS sd-work -quota 100g
mkdir /Volumes/sd-work/worktrees /Volumes/sd-work/retained
ln -s /Volumes/sd-work/worktrees "$HOME/.local/share/sd/worktrees"
ln -s /Volumes/sd-work/retained "$HOME/Documents/sd-backups/worktrees"
```

Recheck the container identifier, current free space, and absence of both symlink
destinations immediately before running those commands. Never replace an existing
path. `runner.sh preflight --config FILE` must then report `ok: true` and
`dispatch_allowed: true`. `runner.sh install-plan --config FILE` emits the exact
LaunchAgent and interpreter for review.

### Change the quota

`diskutil` cannot change an APFS quota in place. A new quota means deleting and
recreating the volume, which destroys every worktree and retained clone on it.
This README gives no copy-and-verify procedure: a file count, or a hand-written
inventory, can match a truncated copy or one without permissions, ownership,
ACLs, or extended attributes. Before deleting the volume:

- Stop the runner: `launchctl bootout gui/$(id -u)/<prefix>.sd-runner`.
- Hold a backup whose restore you have verified completely, metadata included.
- Keep that backup until the restored volume passes `runner.sh preflight` and the
  runner has resumed its queue.

## Queue and controls

Run `sd runner prepare ITEM --branch BRANCH` inside the registered repository to select its branch.
Existing local or remote branches preserve the accepted sync rules.
A new ordinary branch is bound to the registered remote's default commit; a
changed default or a colliding new branch refuses dispatch. The owned clone
publishes the new branch; the operator checkout is never changed.

Use `sd run --sequential ITEM...` or `sd run --parallel ITEM...`. Only distinct
branch author work runs in parallel. Exclusive requests wait fairly for existing
authors and prevent later parallel requests from starving them. A sequential
successor waits for verified remote merge evidence, not merely an author exit.
The default budget is 90 minutes. Provider credentials are filtered to the minimal
execution base plus the variables declared for that provider. Actual provider and
vendor are recorded; authored commits must carry matching `Authored-with` trailers.
An attempt with no commit of its own is judged on the commits its assignment's
earlier attempts pushed beyond the first attempt's base, less the default branch.
A later attempt then finishes work an earlier one committed before it timed out.
With nothing carried, the attempt still refuses as `authored no commits`.

An author session leaves two files under its clone's `.git/`, and the runner
reads both before judging its exit code. `.git/sd-notes.jsonl` holds one JSON
object per line, `{"kind": "followup", "body": "..."}` with a kind of
`followup`, `decision`, `proposal` or `question`; each becomes a `note` row on
the item with the run as its session, and a malformed line blocks the run
naming the line rather than dropping it. `.git/sd-stop.json`, `{"kind": ...,
"detail": ...}`, is the session's own hard stop. The three hard stops are the
ones `prd.md` names -- a failing test, a blocking review finding open past the
cap, a write outside the repository -- and the runner decides the first two
itself as well: after a session exits 0 it runs the repository's own check
through the pack's `sd-check --json` in the clone (the failed checks the stop
names come from the `checks` list parsed out of the whole report, not from the
4000-byte tail of output the result keeps), and when `sd-ship prepare`
refuses it reads the receipt for a failed check or a `blocking` report. A stop
ends the row `blocked` with `hard stop: <kind>: <evidence>` as its detail, the
same text in the run's `exec` note and in an open `followup` on the item, and
the branch pushed and left in place.

The environment is not the code, so its failures are not hard stops (sd:1762).
Before the check, the runner installs the clone's declared dependencies:
`npm ci` for a `package-lock.json` or `npm-shrinkwrap.json`, and
`pnpm install --frozen-lockfile` for a `pnpm-lock.yaml`. Before an author
session starts, at least one reviewer of another vendor must resolve on the
search path, because `sd-ship prepare` reviews with one. A missing installer,
a failed install, no runnable reviewer, and an `sd-ship` readiness refusal
coded `executable_missing` each end the row `blocked` with
`environment: <evidence>` as its detail. The evidence names the program.
No followup is filed: fix the machine, then requeue the row.

A clone is new every run, so a `target/` inside it starts empty. A cold Rust
build of a large workspace can use all 900 seconds of the check (sd:1814).
The runner keeps one seed per repository: `<work>/.cargo-seed/<repo name>-<digest>`,
where the digest is the first 12 hex digits of the SHA-256 of the repository's
stored key. Before the install and the check, a clone with a root `Cargo.toml`
and no `target/` gets a copy-on-write copy of that seed as its own `target/`
(`/bin/cp -c -R -p`, times kept). The runner then touches every tracked file
in the clone, so each copied fingerprint is older than every source there.
The clone's own crates always rebuild; dependencies whose sources live outside
the clone, as registry crates do, stay compiled. Nothing is shared while Cargo
runs: each clone builds in its own `target/`, and the `CARGO_TARGET_DIR` and
Cargo `build-dir` settings are left alone. A shared target or build-dir judges
a workspace crate fresh by file times alone, so a clone could run another
clone's binary.
The copy does not depend on the Cargo version. A seed that another compiler
built rebuilds everything: cold, never wrong. A failed copy or touch removes the
clone's `target/` and the check runs cold.
After a check passes, the runner copies that clone's `target/` beside the seed
and renames it into place; a failed check never seeds. A copy between the two
renames finds no seed and builds cold.
The seed keeps only what builds dependencies: each profile's `.fingerprint/`,
`build/` and `deps/`, without the executables in `deps/`. Final outputs never
enter it, so a binary that a later branch removes cannot run from a seeded clone.
The clone's copy is pruned the same way, so a seed written before pruning is safe too.
Keeping the seed is a retention choice: ending a run, `prune-apply` and
`retained-remove` never remove it, and nothing else does either. To clear one
repository's seed, remove its folder; the next passing check writes a new one.
`rm -rf <work>/.cargo-seed` clears all of them. Removing a seed while a clone
copies it leaves that clone cold, never wrong. Seeds count against the work
volume's free-space floor like any other bytes there.

A repository's test and lint overrides live in the pack's block in an
untracked `CLAUDE.local.md`, which `sd-check` reads before any Makefile or
`package.json` (sd:1752). A clone has no untracked files, so after the branch
is checked out the runner copies that one file from the operator checkout.
It copies nothing else, and it adds `/CLAUDE.local.md` to the clone's
`.git/info/exclude`, so the copy is never committed and the clone stays clean.
A branch that tracks its own `CLAUDE.local.md` keeps it, and a checkout without
one leaves the clone as before. When a copy was made, the run's detail and its
`exec` note end with `local overrides copied from <path>`.

A `start`-line provider (`claude -p`,
`codex exec`) is charged one `run` cost row per session, `call_id`
`session:<run id>`, carrying the `usage` and `total_cost_usd` the session's
result envelope reported at exit; the runner writes no cost SQL of its own and
a `url` entry's session is charged by the library per call.

A `url` author is dispatched, not skipped (sd:234 slice 8d): `provider_command`
takes the first entry the role's order names, a `url` entry among them, and
the clone, the retained log and the ending are the same as a `start` run's.
Only the provider step differs. There is no process: `Runner.answer` hands the
prompt a `start` session would read on stdin to `answer_url` in `sd_db.runner`,
which makes the one call through `sd_db.calls.call` -- reserved against the
assignment's `budget_usd` and the bill's cap, call id `url:<run id>` -- and
writes the raw response body to the clone's `.git/sd-provider.log`, the path
the `exec` note names, before the ending is written: a failed write ends the
row `blocked` on the error, never `done` without its work product. The
remaining time budget is the call's timeout, and a spent one is refused
before the wire with `assignment time budget exceeded`, as a supervised step
is. A settled call ends the row `done` with the token
counts and the cost in `detail`; a lost response ends it `blocked` with the
bound the ledger holds; a refused reservation ends it `blocked` with the
ledger's line as `detail`, `budget spent: ...` for a budget, and files the same
line as an open `followup` on the item. Nothing requeues it: only an operator's
`requeue` does. What `call` refuses by name -- a cleartext URL, an unset key
variable, a missing price on a capped or budgeted entry -- ends the row
`blocked` with that refusal and is not checked again here. A `url` reviewer is
still passed over: a chat completion is not the structured review envelope a
headless session is asked for.

A check that passed is recorded before anything is shipped: one `state` row
of kind `check`, keyed by the run id, whose body carries the clone's `head`,
its tree hash (`git rev-parse HEAD^{tree}`), the `exit_code`, the `argv` the
check ran as, the `checks` list `sd-check --json` reported, and `recorded_at`.
A re-run of the same row replaces the record, and a check that stopped the
run leaves none. It exists for sd-review to read (sd:495): `sd-ship prepare`
runs sd-review, whose first step is the same `sd-check --json` on the same
tree, so a clean row pays the whole gate twice; a record keyed on the tree is
what lets the lane trust the runner's run and re-check only a tree it has not
seen -- a commit amended after the check reads as a different tree. Nothing
reads it yet; the writer and reader are `record_check` and `check_record` in
`sd_db.runner`.

`sd runner cancel`, `sd worktree resume`, and `sd worktree restore` resolve the
installed, owned LaunchAgent and its configured interpreter/database. No command
search guesses a system checkout. Native controls bind the assignment revision and
exact run UUID with `--if-revision` and `--expected-run`. Historical restore keeps
`--run N` as the attempt counter. An existing restore destination is never
replaced; replay is accepted only when its durable intent and complete content
inventory match the same run. The restored copy is writable; retained originals
remain immutable.

Cancellation can signal a proven owned group even while the daemon is stopped.
The request stays nonterminal until cleanup verifies survivors, retains or
archives the clone, preserves outputs, and releases the lease. Resume records a
durable action and requeues in that same final release transaction. Dirty or
unpublished work remains at its kept path and in a complete archive.

`runner.sh commands` and `sd runner commands` expose the same finite command catalog.
Mutating commands use an exclusive assignment and its owned supervisor.
The runner verifies catalog hashes, executable hashes, typed values, and the original authorization note before dispatch.
Commands preserve the item's status and complete their original output note.
They do not require author commits or call the ship adapter.
Dirty output keeps its clone and lease until the operator resolves it.
Resume retains resolved command output without replaying its single-use authorization.

## Recovery and delivery

`runner_run` records setup and ending checkpoints, supervisor PID/process group
and kernel start identity, ownership, quarantine, and output preservation. Each
update also has an fsynced checksum-protected record beside the database in
`runner-journal`. Restoring an older database never rolls that directory backward.
A missing active ownership record, a newer external record, conflicting same-version
records, or a restore-intent marker holds dispatch for recovery reconciliation.
The process group is signalled only while its kernel identity still matches.
Escaped marked children and unrelated clone holders quarantine the repository;
strangers are reported and never killed.

`runner.sh recovery-plan` reports ownership discrepancies and exact fingerprints without changing database rows or journals.
Stop the daemon before `runner.sh recovery-reconcile --run UUID --fingerprint SHA256`.
Reconciliation acquires the daemon lock, checks process holders, and preserves the original evidence in `runner-reconciliation/`.
It can regenerate a missing journal or import a newer journal into its original assignment.
Imported active runs enter blocked cleanup; reconciliation never infers item delivery.
Missing assignments and ambiguous same-version conflicts require a compatible database backup.
Those cases remain held, and their external journals remain unchanged.
`sd-db.sh item remove` and `sd-db.sh repo remove` are the one other thing that moves a journal pair: after their commit they move each removed run's `<run>.json` and `<run>.lock` out of `runner-journal/`.

`recovery-plan` also lists partial, malformed, and unknown journal entries under `journal_issues`.
The normal journal reader remains strict; an issue never supplies ownership authority.
An issue named for a run withholds only that run's entry; every other run's entry still shows beside it.
Reconciliation still refuses every run while any issue stands.
Pause the daemon before selecting an issue for quarantine.
For an unknown file, also stop the application that writes it; runner locks cannot exclude unrelated applications.
Run `runner.sh recovery-quarantine --entry NAME --fingerprint SHA256` with the exact values from the current plan.
`recovery-quarantine` requires the daemon lock and, for run entries, the matching journal writer lock.
The remove verbs list a released run's healthy pair under that run's `runner-ending` lock, one run's lock at a time, and rename it under its journal writer lock alone, all inside `control_gate` and without the daemon lock: the live runner holds the daemon lock for its whole life, and nothing writes a released run's journal once its row is gone.
Busy locks, changed fingerprints, unsafe ownership, and symlinks refuse without moving the selected file.
Healthy JSON and lock entries cannot be quarantined by `recovery-quarantine`.
The operation moves one file into a new private directory beneath `runner-recovery-evidence/`, preserving bytes and mode.
Its receipt records the original name and fingerprint; existing evidence is never overwritten.
The remove verbs are the second producer under `runner-recovery-evidence/`: `removed-<fingerprint>/` holds each removed run's pair and a `receipt.json` that names the record item, the fingerprint, and each file with its sha256.
These recovery archives are included in normal database backups.

A journal file that an earlier restore left linked to its restore evidence is a blocked issue, which quarantine refuses.
Run `runner.sh recovery-unlink --entry NAME --fingerprint SHA256` for it; it acts only when the file's one other link is that restore evidence copy, and it gives the evidence its own copy of the same bytes.
Unlink creates no directory and no receipt; it replaces only the evidence name, and the live file keeps its inode, bytes and mode.

Run `recovery-plan` again before reconciling ownership or restarting the daemon.
Quarantine does not resume work, reconstruct missing rows, or resolve a database restore hold.

`runner.sh status` prints the heartbeat state as one JSON line and answers with convention 6's codes: 0 healthy, 3 when the `<prefix>.sd-runner` agent is not loaded (`launchctl print` fails — nothing to check), 1 when the heartbeat is stale or unhealthy.
The heartbeat's `runner_commit` is the checkout's HEAD when `serve` started: Python keeps the modules it loaded, so a `git pull` reaches the daemon only through a restart (sd:1952).
`runner.sh status` and `sd runner status` compare it with the checkout's HEAD on disk, with no fetch, and add `checkout_commit`.
A moved checkout adds `deploy_warning`, `runner started at <sha7>, checkout at <sha7>; restart to deploy`, and leaves the exit code alone.
A heartbeat without the field, from an older daemon, reads `runner_commit unknown`.
A missing interpreter follows the same split: 3 when the agent is not loaded, because that machine never provisioned the pack; 1 when it is loaded, because a runner whose virtualenv was rebuilt cannot start.
The agent question is decided before the store is opened, so a machine that never installed the agent and holds no store answers 3, not `connect`'s 1.
`local-health-check` reads those codes in its nightly sweep and quotes the first stdout line as the finding, so the body is one line; it is the heartbeat state whenever the store can be read.
When the agent is not loaded and the store cannot be read, the body is `{"ok": false, "reason": "no database at ..."}` (or the read error) and the code is still 3; when the agent is loaded, an unreadable store is broken and exits 1 with the error on stderr.
Runtime probe and cleanup failures produce an unhealthy heartbeat while the daemon retains ownership of active workers.
Unknown measurements forbid dispatch. Confirmed low database space still attempts to stop every owned group.
One failed process observation cannot abort the remaining stops or trigger unrelated restart cleanup.
Database corruption remains fatal; SQLite contention retains its bounded retry behavior.

The agent's err log (`~/Library/Logs/<prefix>.sd-runner.err`) starts every line `serve` or `once` writes with the local time, `2026-10-03T04:05:06+0200`.
It also gets one line each time the heartbeat changes health: `runner: unhealthy: <reasons>` and `runner: healthy again` (sd:1953).
The line names the state at the end of a tick; reasons that change while health stays the same write nothing.
Output a child writes to the file descriptor directly, and an error before `main` starts, carry no time.

### Deploy a change to the running daemon

Python reads the runner's modules once, at start, so a merged change reaches the daemon only through a restart.
Pull the checkout, then run `runner.sh restart`. Do not kick the agent with `launchctl` by hand.

The verb first drains the daemon, because an idle queue read before the kick can be claimed before it.
It writes `runner-drain.json` beside the database, and each pulse reads it before its tick claims anything.
A heartbeat that names the marker's token says the daemon claims nothing more.
That heartbeat must come from the pid `launchctl print` names for the agent.
So a `--config` naming a database the agent does not serve refuses, and the agent is not kicked.
The verb holds `runner-restart.lock` beside the database from before the marker to after its removal.
The daemon honours the marker only while some process holds that lock.
So the drain has no expiry: a machine sleep or a slow `recovery-plan` scan cannot end it.
The kernel drops the lock when the verb dies, and the daemon then ignores the marker; the queue does not stay stopped.
The verb writes its marker once and never renews it, so an ended drain cannot come back.
Just before the kick the marker must still name the verb's token.
Otherwise the verb refuses: `the drain marker was removed or replaced before the kick`.
One restart runs at a time: a second verb refuses with `another restart is running`.

The verb then refuses with a reason, removes the marker, and leaves launchd alone, unless all three guards pass:

- No assignment is active. A kick ends the daemon that supervises it.
- `runner.sh recovery-plan` is clean. The new daemon's recovery holds on what the plan lists, so it would start unhealthy.
- The 1-minute load average is below `--max-load` (default: the core count).
  A cold start under load stalled on `diskutil` and launchd relaunched it for six minutes (sd:1950).

It then runs `launchctl kickstart -k` on the `<prefix>.sd-runner` agent.
It waits up to `--wait` seconds (default 180) for a healthy heartbeat with a new pid.
It removes the marker, prints that pid and the heartbeat's `runner_commit` (null from a daemon that does not write it), and exits 0.
Otherwise it exits 1 naming the reason.

A daemon that started before the drain existed never acknowledges it, so the verb refuses.
Deploy that first change by hand, when the queue is idle.

The runner refreshes kept-clone archives on a persisted 24-hour cadence.
Each refresh creates a separate verified generation and preserves the original archive and all previous generations.
Process holders, restore uncertainty, any standing `journal_issues` entry, changing contents, and insufficient free space hold the refresh.
The journal hold names each entry beside the verb that accepts it: a blocked entry needs recovery, an unblocked one quarantine, as under `recovery-plan` above.
`runner.sh archive-plan` reports its schedule; `runner.sh archive-refresh` runs the same guarded backup operation.
Restore selects the latest verified generation when no retained clone exists.
An interrupted restore stays bound to its original inventory when that generation remains available.
No archive or clone is deleted by this scheduler.

The supervisor receives no effects before registration and acknowledgement. It
is closed and joined before ending begins. A still-live supervisor remains
running and held until the service can join it. Every ending step is replayable;
terminal assignment state and lease release are last.

Clean clones move atomically to a retained path and get APFS immutable flags.
Ignored deliverables are copied from the frozen clone into that run's `ignored/`
directory before release and outlive clone retention. Known caches are excluded
from that separate output copy, and so are Cargo build output (a `target/` beside
a `Cargo.toml`) and the runner's own root `CLAUDE.local.md`; the retained clone
still holds them. A manifest stored before that rule is replayed through the
same rule, and the ending stores the smaller manifest, so the run journal shrinks
too. The inventory, copy and archive refresh the heartbeat at most once per
interval, naming the run in `ending` and the last probes' time in `probes_from`,
so a long copy does not read as a stale runner. This implementation does not delete caches
without the retention deletion policy being approved. Whole-clone archives retain
ignored files, reflogs, stashes, staged/unstaged state, merge state, and local
objects; they leave out known caches and Cargo build output only where Git reports
them untracked and ignored, so a tracked file under such a name keeps its unstaged edits.
Before the ignored copy or the archive writes, the ending estimates its bytes. A
copy that would leave less than `free_floor_gb` free waits in `ending` naming
`no space`, as a copy that met `ENOSPC` does. Archive writes use a stable inventory, listback, fsync, and one rename
under a per-run lock. Restore validates archive paths and creates symbolic links
without traversing their targets.

Hooks are copied with their files, helper layout, and modes. The accepted PRD's
explicit exception applies to absolute hook paths outside the checkout: those
remain external (`prd.md`, requirement 1, lines 61–67). Parallel guards forward
every effective hook and restrict ordinary pushes to the leased branch. Hooks
are protection against mistakes, not an adversarial sandbox (`--no-verify` bypasses
a Git hook).

Author work calls the real `sd-ship prepare` adapter. Automatic repositories queue
a separate exclusive merge assignment; authors never merge. A changed combined
head must earn the adapter's bounded exact-head verification before merge.
Manual-policy PRs are observed without changing the operator checkout; a confirmed
merge queues a fresh owned reconciliation clone to prove default-branch ancestry.
Whole-item delivery is two phase: prove Git evidence while the clone is mutable,
retain it, then finalize the stored revision-bound proof inside lease release.
There are no network operations against a frozen clone. Slice delivery records
progress while keeping the item open.

## Verification and remaining approval boundaries

Run `runner.sh test -v` for real bare Git repositories, controlled native process
groups, crash checkpoints, recycled PID refusal, escaped children, ignored output,
archive/restore, queue controls, and storage policy tests. CI runs this on Linux.
Run `runner.sh test-macos -v` on a Mac for what only macOS has: APFS immutable
retention (`chflags uchg`), clonefile cargo seeds (`cp -c`), `retained-remove`,
`prune-apply` and the ship lifecycle. Those modules live in `tests/macos/`, and
`tests/run-macos-only.sh` at the repository root runs them.
The native process checks need host permission to run `ps`/`lsof` and inspect or
signal those fixture groups. A denied inventory holds cleanup; it is not treated
as evidence that no process exists.

`runner.sh prune` produces an exact read-only plan for released clones retained at least 30 days.
The plan binds database ownership, content, age, process holders, and independently preserved ignored output.
`runner.sh discard-plan ASSIGNMENT --if-revision REVISION --expected-run UUID` plans retained handling of a kept clone.
Both commands delete nothing.

`runner.sh prune-apply --fingerprint FP --who NAME [--days N]` is the operator's
approval of one prune plan (sd:770). It plans again and refuses unless the
fingerprint matches. Then it files one `runner-prune` report, moved to `done`,
with every entry's run, path, bytes and proof, and removes the retained clones
the plan lists. It removes nothing else: `retention.json`, `ignored/`,
`kept.tar` and `archives/` stay, and a clone younger than 30 days is never in
the plan. So a repo or item remove refused for a retained clone goes through
after `prune-apply` only for a clone the plan lists. A younger clone still
needs the two commands that refusal prints.
The plan it makes again takes each run's `.archive.lock` and waits for it, as
`prune` does, so an archive refresh or restore in progress holds it up.
For each entry it then takes `runner-ending/<run>.lock` and the run's
`.archive.lock`, both without waiting, and neither through a link. It checks
the run row, the clone's device and inode, and its process holders again.
Every entry of the tree must be on the run directory's device. A volume
mounted inside a clone makes the plan refuse and name the mount; one mounted
after the plan makes apply skip that entry before it changes anything. It clears the user immutable
flag on the clone directory and renames it to `.pruning-clone`, then clears
the flag entry by entry without following links and removes the tree,
checking the device before each change. An entry whose lock is held, or that
stops part way, is reported as skipped, at the path its tree is under now. A
`.pruning-clone` left behind is the next plan's `finish` entry, and `restore`
and the sd-db remove verbs refuse while it stands. It runs beside a live
runner; it refuses off macOS, and refuses an empty `--who`.
The record lists what the plan selected and is `done` once filed; what was
removed or skipped is in the JSON `prune-apply` prints, and exit 1 means an
entry was skipped. A plan whose record would pass the 200,000-byte report
bound is refused before anything is filed or removed. How many entries that
allows is not a constant: each entry repeats its clone path several times, so
the count falls as the retention paths get longer. Measured on a one-run
fixture, one entry's whole record was 2,218 bytes under a 42-character
retention root and 2,554 bytes under a 154-character one, which is about 90 and
about 78 entries: divide the bound by one entry's record for the count a given
root allows. A larger `--days` narrows the plan
below the bound, and the older clones go first.
A holder check that fails or times out skips its entry and apply goes on to
the next. If the rename fails, the clone directory is frozen again.

`runner.sh retained-remove --assignment N --who NAME` removes one released
assignment's whole retained copy, `<retention root>/N`, before the 30-day floor
(sd:1780). It refuses unless `--who` names someone, every run row of N is
released, the recovery plan has no journal issue, pending restore, or entry for
N's runs, and N is a real directory inside the retention root, not a link. It
takes each run's locks, checks the process holders, and files one
`runner-retained-remove` record with the actor, as `prune-apply` does. Then it
removes only the attempt directories `N/<run>` of the runs it checked: it
renames each clone to `.pruning-clone`, clears the user immutable flag without
following links, and removes that tree; the same command finishes a stopped one.
`N` itself goes only by `rmdir` once empty. A run claimed after the check owns
an attempt this command never touches, and anything left is listed as `left`.
It prints the assignment, path, bytes freed and actor. It leaves the
repository's Cargo seed under `<work>/.cargo-seed` in place. The command is narrow
enough to allow-list in Claude Code:
`Bash(~/repos/system/local-sd-runner/runner.sh retained-remove:*)`.
That one rule covers both scopes below.

`runner.sh retained-remove --clone-only --assignment N --who NAME` removes less
(sd:1793): only each checked attempt's `clone`, and a `.pruning-clone` an
earlier stop left. `kept.tar`, `archives/`, `ignored/`, `retention.json`, each
attempt directory `N/<run>` and `N` itself all stay, and every user immutable
flag on them stays set. This is the scope the raw `chflags -R nouchg` and
`rm -rf` on one clone had. Every refusal, lock, recheck, holder check and
one-device check above still runs first, and the record is filed the same way.
The record and the printed JSON carry `"scope": "clone"`, the paths removed
(`removed`) and the paths each attempt keeps (`kept`); without the flag they
carry `"scope": "attempt"`. A run that finishes a stopped one removes the
`.pruning-clone` and files its own record naming it. With no clone left, the
command removes nothing and files no record.

Volume provisioning, LaunchAgent installation/activation, and any other clone
or output deletion require the operator's concrete approval. Metered USD budget
redesign is deferred by the operator; the elapsed-time budget remains enforced.
