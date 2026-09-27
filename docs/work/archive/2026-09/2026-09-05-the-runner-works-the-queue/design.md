# Design — the runner works the queue, on one page

The `prd.md` beside this file is the decision trail, near two thousand lines
with forty rounds of findings answered. This page is what the runner is
checked against: each requirement in a few sentences, written on 2026-09-05
at the operator's request. When the two disagree, the `prd.md` is right and
this page is fixed.

## 1. What the runner is

`local-sd-runner/`, a launchd service, `local.system-tools.sd-runner` in
`personal.agent`. It reads and writes only through B's library, in B's
vocabulary: `assignment` rows, the `runner` heartbeat row, `merge_policy`
on the repository row. Item A's `sd-ship` is the merge path it calls and
item A's registry names the providers it starts. It watches rows with
status `queued` and turns each into a session. It never asks a question; it
records what it decided.

## 2. The clone is the isolation, and the only isolation

Every row runs in a clone of its own under
`~/.local/share/sd/worktrees/<item>/<assignment>`, fresh, never reused,
never the operator's checkout: `git clone --reference <checkout>
--dissociate`, the checkout's store read once and never again, no
`alternates`, its own refs and its own fetch. A linked worktree was the
serial row's until round eight and it goes, because two worktrees of one
checkout share every ref and the object store. The cost of isolation is one
copy of the reachable objects per row, and that is the price.

The clone starts at one commit the row records: the remote's head of the
item's branch. A checkout strictly ahead is fast-forwarded to the remote
first; a diverged pair refuses the row naming both heads; a branch only the
checkout has is published with a lease; a branch neither has refuses the
row. Before the session starts the runner merges the default branch as it
now stands into the item's branch — a merge and never a rebase, because a
rebase loses item A's attribution trailers, and the merge commit never
reaches the default branch since `sd-ship` squashes. A conflict ends the
row `blocked` with the files named and starts no session.

The checkout's effective hooks directory, `git rev-parse --git-path hooks`,
is copied into the clone at dispatch, every file and subdirectory with its
mode and layout except the `.sample` files, and recorded on the clone's
config as `sd.hooksForward`. A checkout with no hooks copies nothing and
that is not an error. A hook that hard-codes the checkout's absolute path is
the stated limit.

## 3. The claim comes before every effect

Nothing runs under a row that does not say `running`, and nothing exists
that no row owns. The claim's transaction writes `running` with
`start_step` `claimed` before any file or remote is touched. The runner
then spawns a supervisor and records its pid, group and start time,
`supervised`. Every setup effect — the clone, the branch's publication, the
dispatch merge — runs inside that supervisor's group on a line from the
runner, recorded as `cloned`, `branched`, `merged`. The provider starts on
one acknowledgement line written only after `started` is committed, so a
runner that died anywhere leaves a `running` row whose group the restart
rule kills.

On start, for every `running` row: kill the group when the leader lives and
its start time matches, list the members when it does not, then run the end
run. The outcome is `blocked` with a `runner restarted` note, and nothing
dispatches for that item until the operator requeues. A recycled pid is
never killed. One `running` row per repository is the serialization, and
the restart rule keeps it true.

## 4. The end run is journaled, idempotent, and destroys nothing

When a session ends, one transaction moves the row `running` to `ending`
and records the outcome the end will carry. Nothing else is written to the
row until cleanup finishes. The steps — survivor check, durability gate,
push, retention, copy of ignored files, release — each record `end_step`,
each is idempotent, and the terminal status is written last, in the
transaction that releases the repository. A row is `ending` exactly while
something remains to do.

The durability gate is the same for every ended row: the branch's head on
the remote, or, for a `merge` row, the merge confirmed by GitHub. When the
head is not there the runner pushes; a failed push keeps the worktree and
archives it.

No clone the runner made is ever unlinked on the queue's path. Once the
gate passes, the clone's directory is renamed in one `rename` into
`~/Documents/sd-backups/worktrees/<assignment>/<run>/clone/` and made
immutable with `chflags -R uchg`. Clean and pushed speak of the working
tree and the item's branch alone, never of a stash, a side branch, or a
reflog-only commit the clone still holds. Removal belongs to the nightly
job, thirty days later, off the path any queued row waits on.

Ignored files are not disposable. Disposable is an allow-list of caches the
runner carries plus whatever the repository declares under `disposable:` in
its `CLAUDE.local.md`; those are unlinked before the retention. Every other
ignored file is copied to `<assignment>/<run>/ignored/`, where `run` is a
counter every requeue increments, so a second run never writes over a
first's only copy.

## 5. Survivors quarantine the repository; the runner kills nobody

Before releasing a lease the runner looks for survivors and tells the row's
own from strangers by three marks: process group, `lsof` on the worktree,
and `SD_ASSIGNMENT` in the environment. A sibling's hold counts for
nothing. A stranger's hold quarantines the repository exactly as an own
survivor's does, because an escaped child and the operator's editor look
alike from outside, and the runner cannot tell them apart. The row stays
`ending`, the lease held, nothing dispatched into that repository. Today
names the pid with its command line. The runner kills a stranger never.
Containment is that the repository is not released while anything the
runner can see still holds it; removal is cleanup, not containment.

## 6. A dirty worktree is kept, and everything about it is archived

With no survivor and the worktree dirty, the worktree stays exactly as it
is and the whole clone directory, `.git` included, is archived to
`<assignment>/<run>/kept.tar` — written as `.partial`, listed back, renamed
in one `rename`, under an `flock` every writer and `restore` holds. The
archive is taken only when a walk before and after agrees on every path's
size and mtime, so a `tar` never carries a half-written index. A directory
is what is in the directory: the index split or not, an unresolved merge
with its stages, a stopped rebase, the LFS store, the stash, the reflogs,
every unpushed commit. That replaced the snapshot-ref and bundle scheme of
rounds nineteen to twenty-four, because no list of Git-internal pieces ever
ends.

Two ways out, both on the item screen and in the terminal, and both the
same journaled end run: `discard` removes the worktree, `resume` requires
it clean and requeues on a fresh clone after the push confirms. `sd
worktree restore <assignment>` rebuilds a lost worktree from its archive;
`restore --run <n>` copies a retained clone, thawed, beside the path and
never over one. A kept worktree holds its branch's lease, so the next row
on that item refuses naming the kept path.

## 7. Storage is a volume, not machinery

The worktrees directory and the backup path live on one APFS work volume
the operator adds once with a quota, `diskutil apfs addVolume <container>
APFS sd-work -quota <size>`; `sd.db` stays on the operator's own volume.
The quota is the bound: a provider gets `ENOSPC` inside it and takes
nothing outside it, the retention's rename stays on one device, and a full
work volume still leaves every row write possible. The runner refuses to
start when the database shares a device with the worktrees, or when the
worktrees and the backup path do not.

`free_floor_gb`, forty by default, is the warning, and it is read against
two volumes. On the work volume: `statvfs` every tick, dispatch stops under
it, Today and one email name the floor and the free space, running rows
finish. On the database's own volume the same floor is read and the
behaviour differs — a tick under it **ends every running row**, because no
`du` measures what a provider writes under `HOME` and the fastest grower
cannot be named there. The volume's quota must exceed the floor, and the
runner refuses to start when it does not, naming both numbers. A retained clone leaves at thirty days
and never earlier, whatever the free space, because a removal that made
room by taking it would spend the copy the retention exists for. The
growth allowance, the reservation, the fastest-grower kill and the ballast
of rounds thirty-four to forty are withdrawn on the operator's word, for
one policy a solo operator can hold in mind. The promise is not that the
volume never fills but that nothing of a run is lost when it does. The
thirty days, the forty and the one work volume are reversible.

## 8. A selection runs as a batch

`Run sequential` and `Run parallel` from the Backlog or Today, or `sd run
--sequential <ids>` and `--parallel <ids>`. Both create one `assignment`
per item, role `author`. Sequential chains them by `after`, and the barrier
is delivery under both policies: the predecessor ended `done` and its
`merge` row recorded phase `merged`. An author's end is completion;
the confirmed merge is delivery, and nothing follows it. Under `manual` the
operator merges by hand and a watch on every `ready_to_send` item creates
the merge row when GitHub says merged, so sequential means one thing under
both policies. Any other ending marks every successor `blocked` naming the
predecessor. A chain is checked acyclic at creation, and a selection is
checked before any row is written: two selected items resolving to one
repository-and-branch pair refuse the whole selection, naming both. That is
a selection-time check and not the lease, which refuses one row at a time at
creation and would leave the first of the pair running. An earlier draft of
this section named the acyclic check and not this one, and no pull request
carried it as a result.

Parallel starts every selected row at once, regardless of repository; the
operator chose the set and no cap second-guesses it. Serial, `exec` and
`merge` rows take a repository alone. A parallel row does not start while
an eligible exclusive row queued before it waits, so neither lane starves
the other. A parallel author's clone carries a runner-owned `pre-push`
guard under `~/.local/share/sd/hooks/` that refuses every push but a
fast-forward of the item's branch to its own name — a destination that is
not that branch, and an update to it whose old remote sha is not an
ancestor of the new local sha, `git merge-base --is-ancestor` on the two
shas the hook reads per ref, since a hook is told nothing about `--force`
— and otherwise runs the repository's own hook from `sd.hooksForward`. **Every other hook name in that directory is a
pass-through shim that does nothing but run the repository's own.**
`core.hooksPath` replaces the whole directory and not one hook, so a
directory holding `pre-push` alone would silently remove the repository's
`pre-commit`, `commit-msg` and every other hook from every clone. It guards
a mistake, not an adversary; `--no-verify` passes it, which is the limit
stated.

Three rules, the operator's parallelism doctrine written into the runner:
writers are isolated, each on its own branch in its own clone under a lease
on the repository-and-branch pair; one serial merge lane per repository;
explicit budgets and no silent death, `budget_minutes` defaulting to ninety
and `budget_usd` only when the operator typed one. At the budget the runner
kills that assignment's group alone and the ordinary end run finishes the
row. No row upgrades shared access to exclusive while it runs, and a row
waiting on its `after` holds no turn, so the queue cannot deadlock.

## 9. The merge lane

A row ends at pull-request-ready, always. Under `manual` the item goes
`ready_to_send`. Under `auto` a `merge` row is created, role `merge`, no
provider, and taken when nothing else runs in that repository: merge the
default branch in once, review the combined head once unless it is exactly
the `reviewed_head` the author recorded, wait for CI, merge naming the head
that passed both, `git fetch -p`. Every remote effect is recorded as a
`phase` — `updated`, `ci_passed`, `merged` — and a requeued merge row asks
GitHub for the pull request's state before it acts, finishing from that
evidence alone when GitHub says merged. A head that moved invalidates both
the review and the CI result; a phase completed for one head is no phase
for another. The merge goes through `sd-ship`, which re-asks the remote
whether the repository is the operator's alone and the default branch
protected; a no ends the row `ready_to_send`, `merge_policy`
notwithstanding. An author never merges from inside its own session.

## 10. Pulse, sleep, keys

Every tick writes one `runner` row — pid, time, pack version — replacing
the last; a tick older than three intervals shows the runner silent on
Today, and `local-health-check` runs `sd runner status`, which exits
non-zero on a stale heartbeat. Each session runs under `caffeinate -i -w
<pid>` so idle sleep waits for it; a session cut by forced sleep is handled
as a killed one. The runner's plist sources `~/.config/shell/env.sh` so the
runner sees what a terminal sees, but a session inherits only its registry
entry's named `env` variables plus `PATH`, `HOME`, `LANG`, `TERM`, `TMPDIR`
and `XDG_CACHE_HOME`, the last two pointed under the clone. A session for
one vendor does not inherit another vendor's key; that it can read the
environment file itself is the gap, asserted rather than hidden.

## Landing order

Last, slice five of B's order: B's harness, library and migrations; A's
ship path and protection; B's dashboard, read-only then writing; then this
item. It is built first as a spike, a few hundred lines against B's fixture
harness that run one assignment end to end, and what the spike finds is
recorded on the `prd.md` as findings. Before it lands, an assignment row is
created and shown but dispatched by nothing.
