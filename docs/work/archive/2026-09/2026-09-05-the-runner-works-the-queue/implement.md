---
title: implement — the spike first, then eight pull requests
status: planning
created: 2026-09-05
---

# Implement

A spike, then eight pull requests, in order. The spike is not product and
does not merge; the eight do, each one reviewable alone against B's fixture
harness.

Nothing here builds B's library, B's fixture harness, A's `sd-ship` or A's
provider registry. This item consumes all four.

**This item is slice five and starts last.** B's landing order
(`B/prd.md:1134-1163`, the slice-five sentence at `B/prd.md:1157-1160`,
which opens "5. D: the runner"), this item's
`prd.md:886-889` and `design.md:236-243`
all say the same thing: D opens after B's slice 4 — the dashboard's writes,
the palette, cost and the backbone — is on `main`, not after B's slice 1. An
earlier draft of this page said "after B's slice one has landed the library
and the harness, and after A's ship path is on `main`, which is the order
both `prd.md` files record." It was not, and the sentence authorised opening
PR 1 two slices early. The palette, `exec` rows and control scope this item
executes are B's slice 4, and B's slice 4 says in its own words that an
assignment created there waits `queued` until this slice.

## PR 0 — the spike, which does not merge

**Touches:** a throwaway branch, `spike/sd-runner`, and this item's
`prd.md`.

A few hundred lines against B's fixture harness that take one `queued`
assignment from claim to release: clone, dispatch merge, provider start,
end, survivor check, retention, release. No launchd, no batches, no merge
lane, no storage floor. It exists because prose review reached its limit on
this item — twenty of B's first forty-eight findings landed on the runner,
and rounds twenty to twenty-six went on finding a high-severity defect at
about one and a half per round, each fix a paragraph on a paragraph. The
defects left are about pids, worktrees, remotes and restarts, and those are
found by running the thing.

**One storage question belongs to the spike, because no test can answer
it.** Every `statvfs` assertion in criterion 6 runs against a double, so
nothing in the item exercises the real call. APFS volumes in a container
share the container's free space, and whether `statvfs` on a `-quota`
volume reports quota-remaining or container-remaining is not settled
anywhere in the `prd.md`. If it reports the container's, the floor never
trips on a large disk, the warning half of the storage policy is inert, and
the quota does all the work alone — survivable, but not what the design
promises. The spike creates one small quota-limited volume, fills it, and
records the answer as a log entry. This is the cheapest place to settle it
and the only place before PR 4 depends on it.

**What it is for.** Its output is findings on this item's `prd.md`, written
as log entries in the form the forty rounds already use, and a judgement on
whether the eight pull requests below are still the right cut. The spike's
code is read and thrown away. If it disagrees with the `prd.md`, the
`prd.md` is what changes.

**Verification.** One assignment reaches `done` with its clone retained and
its lease released, run twice from a clean fixture, and a kill at each of
the four recorded setup steps leaves a row the next start reconciles. The
`statvfs` answer is recorded. Every disagreement with the `prd.md` is a log
entry before PR 1 opens.

## PR 1 — the clone, and the lease it already needs

**Touches:** `local-sd-runner/` (new). The `sd worktree` commands are
PR 3's and are named there; an earlier draft listed them here too, and
nothing in this pull request's body builds or uses one.

The clone and nothing that dispatches into it: `git clone --reference
<checkout> --dissociate` on the item's branch, objects copied in,
`.git/objects/info/alternates` asserted absent, its own refs and its own
fetch. The four starting-commit cases — remote's head, checkout strictly
ahead fast-forwarded, diverged refused naming both heads, branch only the
checkout has published with a lease. The hooks copy: `git rev-parse
--git-path hooks` in the checkout, the directory copied whole with modes
and layout, `.sample` files left out, `sd.hooksForward` recorded on the
clone's config.

**Every other hook name gets a pass-through shim, and that is here, not
later.** `core.hooksPath` replaces the whole directory rather than one hook
(`prd.md:734-738`), so writing only a `pre-push` file into the runner's
hooks directory silently removes the repository's `pre-commit`,
`commit-msg` and every other hook from every clone. Criterion 1 asserts a
`pre-commit` marker exists after a commit in a parallel author's clone. The
shims ship with the copy that creates the directory; PR 6 adds the `pre-push`
guard's own body to it.

The dispatch merge of the default branch, a merge and never a rebase, a
conflict reported rather than resolved.

**The lease is defined here.** The branch-only-in-the-checkout case
publishes under a lease, and the lease on the repository-and-branch pair is
unique across every row that is `queued`, `running` or `ending` and every
kept worktree. An earlier draft of this page defined the lease in the
batches pull request and used it in three earlier ones, which meant either a
placeholder that a later PR rips out or three PRs shipping unguarded — and
unguarded is the exact hazard `prd.md:804-805` gives as the reason the lease
exists, because clones bypass git's check that a branch is checked out once.

**Why it is first, and what that claim is not.** Every other pull request
runs in a clone, so the clone is the foundation. An earlier draft went
further and called it "the one part of this item with no database in it,
exercisable against a fixture repository with no rows at all". That is
false: setup records `start_step` as `cloned`, `branched` and `merged` on
the row, criterion 4 kills the runner between two of them and asserts the
row sits at that step, the publication takes a lease, and this PR's own
verification asserts a second assignment on the same item gets a fresh
clone. Rows are in this PR from its first line. It is first because
everything else stands on it, not because it is row-free.

**Verification.** Criterion 1's clone assertions: the operator's checkout
untouched including its uncommitted edits, a second assignment on the same
item getting a fresh clone, an assignment whose branch is checked out in
the operator's checkout with uncommitted edits starting at the remote's
head with those edits absent from the clone. Criterion 1's `pre-commit`
marker in a parallel author's clone.

**Criterion 4's clone assertions, which the closure table credits to this
pull request and an earlier draft of this section did not take.** The
parallel-clone test's `alternates` and byte-identity assertions
(`prd.md:1132-1135`), and the four starting-commit cases
(`prd.md:1140-1151`): the remote's head, the merge row after an author
pushed from its clone, a `resume` at the remote's head, and the fourth the
paragraph above enumerates. The ledger entry C-63 counts three of these,
counting the pushed merge row and the `resume` as one case; the body counts
four. Four is the count this page uses, and C-63's three is recorded as
superseded rather than replaced.

**Criterion 1's hooks assertions, which an earlier draft filed as "not in
the criteria and should be".** They are criterion 1's, at `prd.md:982-991`,
and richer than that draft described: the fixture checkout's hooks under a
local `core.hooksPath` of `.husky/_`, relative, untracked and ignored, the
clone as made having no such directory; the `pre-push` there sourcing a
sibling `h` with no executable bit that writes the marker; **a serial row's
clone running them from its own `.husky/_` with no guard in front and a
parallel row's from behind the guard** with `core.hooksPath` the guard's and
`sd.hooksForward` the copy; and **a fixture with an absolute
`core.hooksPath` outside the checkout running that directory's hook from the
clone with nothing copied**. The last two were scheduled nowhere while the
page called the whole thing an extra.

## PR 2 — the claim, the supervisor, and the restart rule

**Touches:** `local-sd-runner/`.

The ordering that leaves no window: `claimed` written before any file or
remote is touched; the supervisor spawned and its pid, group and start time
recorded as `supervised`; every setup effect run inside that group on a
line from the runner and recorded as `cloned`, `branched`, `merged`;
`started` committed; and only then the one acknowledgement line down the
pipe that starts the provider. The supervisor as a process-group leader
that outlives nothing it started. The restart rule: for every `running`
row, kill the group when the leader lives and its start time matches, list
the members when it does not, then run the end run.

**Why it is its own PR, and what it can and cannot assert.** This is the
ordering the whole design rests on. It can assert every kill point up to
the moment the end run is needed: a kill at each recorded step, and that no
provider ever ran under a row that did not say `running`. It **cannot**
assert what happens next, because "then run the end run" is PR 3's. An
earlier draft claimed PR 2 was "the part a test can assert without any of
the end run being written" and then listed assertions that need PR 3's
retention and PR 6's lease release. The kill assertions that need a
completed end run are named against PR 3 in the closure table instead, and
**that includes the round-forty kill-during-push clause**
(`prd.md:1110-1116`), whose assertions are about the release — "the next
runner start killing the group before the release", "no process of the setup
alive after the release" — and so cannot be taken here. An earlier draft left
it named by neither pull request while the closure table credited it by
category.

**A clean stop drains before it exits.** Criterion 1's last sentence
(`prd.md:1053-1055`) stops the runner cleanly and asserts every claimed
child is gone before the runner exits. No pull request named a stop path;
PR 8 has the plist and `KeepAlive`, which is restart and not shutdown. It
belongs to the supervisor, so it is here.

**Verification.** Criterion 4's kill assertions up to the end run — a
runner killed after the claim and before the supervisor, and inside each
setup step, each leaving a row whose `start_step` names where it stopped —
plus a test that the pipe closing before the acknowledgement line leaves the
supervisor having started nothing. A recycled pid is never killed, asserted
with a start-time mismatch.

## PR 3 — the end run, and the device rules it depends on

**Touches:** `local-sd-runner/` in `system`; the `sd worktree
discard|resume|restore`
commands.

`running` to `ending` in one transaction recording the outcome; then the
steps, each idempotent, each recording `end_step`: survivor check,
durability gate, push, disposable unlink, an `lsof` check over the clone,
retention rename with `chflags -R
uchg`, ignored copy to `<assignment>/<run>/ignored/` skipping the path
`sd.hooksForward` names, release.

**The `lsof` check before the retention is new, and it is what makes the
retention mean anything.** `chflags -R uchg` does not revoke a descriptor
already open — `UF_IMMUTABLE` is checked at `open(2)`, not at `write(2)`, and
D's round forty-two ran it: `chflags rc=0`, then `write through open fd
SUCCEEDED`, then a fresh open `EPERM`. So a process holding a descriptor
across the retention writes into the "immutable" clone after the archive and
the push gate have both run. The check is the one the nightly removal already
makes before it records `deleting`, and the refusal is the one the end run of
a dirty row already makes: the row stays `ending` naming `clone busy` and the
paths, and completes on the tick after the holder releases. A descriptor
opened between the check and the flag still writes; that window is stated as
a limit rather than closed. The terminal
status written last, in the transaction that releases the repository. The
kept-worktree path: the whole clone directory to `kept.tar` under an
`flock`, written as `.partial`, listed back, renamed; the walk before and
after that must agree. `discard`, `resume` and `restore` as the same
journaled end run.

**The two `st_dev` refusals ship here, not in PR 4.** The retention step
is a rename, and `prd.md:175-179` says the worktrees directory and the
retained path must share a device because a rename across two is no rename.
The refusals are the thing that makes the rename safe: the runner refuses to
start when the database shares the worktrees directory's device, and when
the worktrees directory and the backup path do not share one. An earlier
draft shipped the rename in this PR and the refusals in the next, which
lands the rename on every machine whose backup path is not yet on the work
volume — that is every machine, since the volume is PR 4's operator setup —
and `EXDEV` mid-end-run leaves the clone at neither path. That is a direct
hit on the paragraph below.

**Which makes the volume this pull request's prerequisite, not PR 4's.** D's
round forty-two read `st_dev` for every path the rule names on the machine as
it stands: `~`, `~/.local/share`, `~/Documents` and `/private/tmp`
are all `16777229`, one device, and `sd.db` sits on the operator's own volume,
which is that one. So the first refusal — the database must not share the
worktrees directory's device — fires on the very first start, and this pull
request's Verification needs a runner that starts. The operator's
`diskutil apfs addVolume <container> APFS sd-work -quota <size>` and the two
paths configured under it move here with the refusals. PR 4 keeps the floors
and the quota-versus-floor refusal, which are about how much room the volume
has and not about whether it exists.

**The `ENOSPC` path ships with the copy that can hit it.** The ignored copy
is this PR's step, so the behaviour criterion 6 asserts — the row staying
`ending` naming `no space`, its clone present and its partial copy in place,
completing on the tick after space returns — is this PR's too. An earlier
draft put the step here and the test in PR 4, which would have forced PR 4
to reach back into this PR's end run to make its own verification pass.

**The one thing to get right.** No clone the runner made is ever unlinked
on the queue's path. The disposable allow-list is the single exception, and
it is an allow-list precisely because a name is not a promise — a session
can leave its only deliverable under a directory called `dist/`.

**The remote double that blocks inside `git-receive-pack`, which no pull
request had.** `prd.md:1110-1111`'s "a `git push` held open by a remote
double" needs a fixture that accepts the connection and then blocks
indefinitely, so the push is a live child at kill time. Every other fixture
remote in the item is ordinary — one that gains a branch between the check
and the push (`prd.md:1149-1150`), one that refuses the push
(`prd.md:1328`), one that squash-merges (`prd.md:1163`). The blocking double
is new work and it lands here with the assertion that needs it. **The
mechanism for "ran from the clone and not from the checkout" is the double's
own record** of the client's `GIT_DIR` and working directory: the pre-push
guard's refspec record (`prd.md:956-960`) would also serve, but the guard is
PR 6's and lands after this, so the double is the only one available at this
point in the order.

**Verification.** Criterion 1's cleanup assertions, including the kill point
whose next runner start finds the row `running` with its leader gone and
marks it `blocked`. Criterion 4's kill assertions that need a completed end
run: the next start leaving whatever stands at the path retained with the
lease released, and the assignment's next dispatch cloning afresh at an
empty path — that is `prd.md:1106-1109`, the round-thirty-nine clause.
**Plus the round-forty clause beside it** (`prd.md:1110-1116`), which an
earlier Verification stopped one clause short of: a runner killed while the
branch's publication is a `git push` held open, the push's process in the
supervisor's group, the next start killing the group before the release, the
double asserting the push never completed, no process of the setup alive
after the release, and the push having run from the clone and not the
checkout. It straddles three pull requests — PR 1 publishes the branch, PR 2
puts the push in the supervisor's group and owns the restart rule — and it
lands here because this is the last of the three and the only one that can
run the whole assertion, which is this page's rule at "No criterion is
claimed 'whole' by a pull request that cannot reach all of it". Criterion 6's `ENOSPC` assertions. Both `st_dev` refusals —
the `prd.md` has two rules, at `prd.md:175-177` and `prd.md:380-382`,
asserted at `prd.md:1299-1301` and `prd.md:1353-1354`. An earlier draft
called them three, three times over, having read "the three paths" in the
second rule as a count of refusals.
Plus the archive's quiet-clone rule, which is broader than the example an
earlier draft gave: `prd.md:348-356` makes **any** disagreement between the
two walks discard the partial and try again, and criterion 6's assertion at
`prd.md:1307-1310` is a test writing into the clone, not a `git gc`.
**Plus the rest of criterion 6's body, which the closure table credits to
this pull request and an earlier draft of this section did not take**
(`prd.md:1251-1286`): the retention assertions, `sd worktree restore`, and
the split-index, LFS, rebase and stash variants of the archive. **And, as a
separate range forty lines past the end of that one**, `prd.md:1333-1334`: a
dated database file naming a retained clone the nightly job has removed makes
`restore` report the branch on the remote. It does not straddle into PR 4 —
the removal is only the fixture's premise, which a test sets up by deleting
the clone and leaving the row naming it, and every asserted behaviour is
`restore`'s. An earlier Verification cited a range that excluded it while the
closure table credited it by category. **And
criterion 4's control-scope runner half**, which dependency 3 assigns here:
a row written `ending` by B's dashboard kill stays `ending` while the runner
is stopped, and the runner's first tick after starting writes the archive
with the dirty files in it, releases the lease and writes `blocked`; a
quarantined repository is lifted on that same first tick and a row queued
for it starts.

**The `ENOSPC` path landed 2026-09-12 on `fix/runner-enospc`, separately
from the rest of this pull request, which #227 carried.** A slice map found
no `ENOSPC` in `local-sd-runner/` or `local-sd-db/`: the end run's
catch-all held the row as `cleanup held: ...` with no word for space and no
number the operator could act on. Now `local-sd-runner/sd_runner/storage.py`
raises `NoSpace` (`storage.py:23`) from the two second copies that can meet
it — the ignored copy (`storage.py:92`) and the kept archive
(`storage.py:205`) — when the write itself fails with `errno.ENOSPC`,
naming the copy, the path and the bytes it still has to write. The end run
catches it before the catch-all (`runtime.py:667`) and leaves the row
`ending` with its `quarantine` a record reading `no space` and its `detail`
naming the copy, the space needed, the free space and `free_floor_gb`;
`Runner.space_hold` (`runtime.py:678`) is what reads the free space, and the
heartbeat lists every such row under `space_holds` (`runtime.py:415`), which
is what Today reads for the floor, the free space and the space the copy
needs. The `end_step` stays where it was — `retained_clone` for the ignored
copy, `survivors_clear` for the archive — so the tick's existing retry of
every `ending` row runs the same step again; the partial file
(`ignored.txt.sd-copy-partial`, `kept.partial`) stays where it stopped and is
rewritten on the tick after space returns, and the row releases with
`end_step` at the release. Nothing is removed: the retained clone is
frozen with its ignored files in it, and the nightly job's candidates are
released rows only (`local-sd-db/sd_db/runner_retention.py:16`), so a row
whose copy is waiting is never one. Tests:
`local-sd-runner/tests/test_no_space.py`, three, with the double at the
write (`copyfileobj`, `TarFile.add`) and never a full volume: the ignored
copy held through ten ticks with the clone, the partial, an older released
run's clone and copy all in place, Today's numbers on the heartbeat, and the
copy completing on the tick after space returns; the kept archive held and
then `kept`; two rows ending on a full volume both held, the plan past thirty
days removing neither, both completing. Three readings the documents leave
open, taken conservatively. The hold is written into `quarantine`, the same
column `clone busy` uses, because that is the mark the dashboard renders as a
notice and the one `release` refuses across
(`local-sd-db/sd_db/runner.py:322`); it has the side effect `clone busy`
has — no new claim lands on that repository while the row waits
(`local-sd-db/sd_db/runner.py:167`), which on a volume that just returned
`ENOSPC` is under the floor anyway. The heartbeat stays `healthy` while a
copy waits, as it does for `clone busy`: the floor's email is the operator's
signal, not a nightly finding. And the retention receipt's own write
(`retention.json`, a few dozen bytes) is left as it was: the `prd.md` says a
retention needs no space, and a receipt that meets `ENOSPC` falls to the
catch-all and is retried, but a receipt created empty by that failure would
then refuse `open("x")` on the retry — a stated limit here, not closed.

## PR 4 — the volume, the two floors, and the nightly removal

**Touches:** `local-sd-runner/`, `sd-db-backup` (B's), `local-health-check`.

`free_floor_gb` read with `statvfs` every tick, dispatch stopped under it,
one email per crossing through B's requirement 9 path, running rows
finishing. The nightly removal at thirty days: `chflags -R nouchg`, the
recursive removal, the `clone.lock` and `lsof` skips, the `end_step` guard
that keeps a clone whose copy is still waiting.

**The scratch paths land here too, and no pull request had them.**
`prd.md:529-533` puts `<clone>/.sd-run/tmp` and `<clone>/.sd-run/cache` on
the work volume "so that a build's scratch and a package manager's cache …
land under the quota and not on the operator's volume", `prd.md:255` lists
`.sd-run/` on the runner's disposable allow-list, and criterion 6 asserts
both at `prd.md:1354-1357`: a provider script writing under `$TMPDIR` and
under `$XDG_CACHE_HOME` has both files under the clone's `.sd-run/` on the
work volume and absent from the retained clone. `TMPDIR` and
`XDG_CACHE_HOME` are **repointed** at session setup, not merely inherited —
PR 8's registry paragraph passes them through, which is a different thing,
and an earlier draft of this page mentioned `.sd-run/` nowhere at all. This
pull request owns the quota and the floors, so the redirection lands beside
them; PR 3's disposable unlink already removes `.sd-run/` with the rest of
the disposable list, and the assertion that the files are absent from the
retained clone is what joins the two.

**The nightly re-archive lands here too, and no pull request had it.**
`prd.md:461-466` makes the archive one idempotent operation run at four
moments, the fourth being "nightly by `sd-db-backup` for every kept
worktree, because the operator edits in a kept worktree between those
moments and an archive is only as current as" its last run. Criterion 6
asserts it three times, not twice — the job held against a `git commit`
mid-write keeps the previous archive, names the skip on Today and writes a
new one on the next run, and the job against a dirty row's end lock waits and
then archives again, both inside `prd.md:1302-1306`; and a kept worktree
edited after its row ended has the edit in the archive after the nightly job,
at `prd.md:1330-1331`. An earlier draft of this paragraph gave the second
assertion the third one's line range and counted two — a line number written
beside a description that is not at it, which is C-57's class recurring
inside C-54's fix. The third is the assertion that most directly motivates
the re-archive, and it was the one going unnamed. Round one's closure table
credited this to PR 3, whose Touches do not include `sd-db-backup`; this
pull request is the one that edits that job, so it is the one that can add
the re-archive beside the removal.

**There are two floors, and the second behaves differently.** The floor is
read against the database's volume as well as the work volume
(`prd.md:418-420`, `prd.md:536`, criterion 6 at `prd.md:1358-1360`), and a
tick under *that* floor **ends every running row** rather than letting them
finish (`prd.md:2027-2029`), because no `du` measures a write under `HOME`
and the fastest grower cannot be named there. Neither `design.md` nor an
earlier draft of this page mentioned the database's volume at all, so the
one behaviour that distinguishes the two floors was being designed out
silently.

**The quota must exceed the floor, and nothing said so.** `<size>` is the
operator's to fill and no stated constraint relates it to `free_floor_gb`.
A volume created at forty gigabytes with the floor at forty reports free
space at or under the floor from the first tick: dispatch stops
permanently, one email is sent, and the queue never starts with no error
naming why. The runner refuses to start when the work volume's total size
is not greater than `free_floor_gb` by a stated margin, alongside the two
device refusals, and names both numbers. The doubling of the floor to forty
widened this: at twenty, plausible volume sizes cleared it comfortably.

**What this PR deliberately does not add.** No growth allowance, no
dispatch reservation, no fastest-grower kill, no ballast file. All four
were designed across rounds thirty-four to forty and all four were
withdrawn on the operator's word. The quota is the bound and the floor is
the warning. A reviewer will re-raise unbounded provider writes; the answer
is the volume quota, and it is the answer on purpose.

**Verification.** Criterion 6's storage assertions other than `ENOSPC`,
which is PR 3's: the `statvfs` double under the work volume's floor, the
double under the database volume's floor — which criterion 6 asserts stops
dispatch and sends one email (`prd.md:1357-1360`) and does **not** assert
ends every running row, the gap C-49 recorded and this pull request does
not paper over — the nightly re-archive's two assertions, two rows
ending on a full volume, the thirty-day job removing neither, the
`restore --run` holding `clone.lock` shared, the start refusal when the
quota does not clear the floor, and criterion 6's scratch-path assertion —
a provider's `$TMPDIR` and `$XDG_CACHE_HOME` files both under the clone's
`.sd-run/` on the work volume and both absent from the retained clone.

**Setup PR 3 needs from the operator, and this PR keeps the rest.** The
volume itself is a prerequisite of **PR 3**, not of this one, from D's round
forty-two: PR 3 ships the two `st_dev` refusals, and on the machine as it
stands `~/.local/share`, `~/Documents` and `sd.db` are one device — all
`st_dev 16777229` — so the first refusal fires on the first start and PR 3's
own verification, which needs a runner that starts, cannot run. So
`diskutil apfs addVolume <container> APFS sd-work -quota <size>` and the two
paths configured under it, `sd.db` left on the operator's own volume, are
listed in PR 3 and are named here only as the thing PR 3 already did. The
size is the operator's and is not in the `prd.md`; `free_floor_gb` forty is,
and the size must exceed it. This pull request's own ask of the operator is
nothing.

## PR 5 — the merge lane and the watch

**Touches:** `local-sd-runner/`.

`merge` rows under `auto`, created at the author's end, taken when nothing
else runs in that repository: update once, review the combined head once
unless it is exactly `reviewed_head`, wait for CI, merge naming the head
that passed both, `git fetch -p`. The `phase` record — `updated`,
`ci_passed`, `merged` — and the reconcile a requeued merge row runs against
GitHub before it acts, which finishes from GitHub's answer alone when
GitHub says merged and needs no clone. The watch on every `ready_to_send`
item whatever its repository's policy, which creates the merge row when the
operator merged by hand.

**Why the watch is not optional, and why this PR now precedes batches.**
Sequential has to mean one thing under both policies, or a chain under
`manual` waits on a merge row nobody creates. Policy decides who may merge,
not who observes a merge. An earlier draft put batches first and justified
it with "a batch with no merge lane still runs under `manual`" — which is
the exact scenario this paragraph calls broken, in the same document. The
delivery barrier is the predecessor `done` **and** its `merge` row at phase
`merged` under both policies (`prd.md:680-683`, `prd.md:693-699`), so the
merge row has to exist before a batch can wait on one. Shipping batches
first leaves every successor in every chain `queued` forever.

**Verification.** Criterion 2's `manual` and `auto` clauses and the merge
row itself — not criterion 2 whole. Its two chained-successor assertions
(`prd.md:1077`, `prd.md:1084-1085`) need `after` and the delivery barrier,
which are PR 6's. Including the hand-merged
`ready_to_send` item producing a `merge` row at phase `merged` with no
second pull request opened, and the closed-unmerged case blocking the item
and its successors. Criterion 4's merge-row barrier assertions under both
policies and the two-items-under-`auto` ordering test.

## PR 6 — the lanes, the batches, and the pre-push guard

**Touches:** `local-sd-runner/` in `system`; `sd run` in
`sd-ai-command-pack`, landing first; the pre-push guard under
`~/.local/share/sd/hooks/`.

**The guard cannot see `--force`, and its rule is written for what it can
see.** D's round forty-two ran a plain push and a `--force` push of one
branch against a `pre-push` hook that dumped its input: byte-identical argv
(`origin <url>`), the same four-field stdin shape, and no force-related
variable in the environment. The two clauses the guard actually implements
are a destination that is not the item's branch, and an update to the item's
branch whose old remote sha is not an ancestor of the new local sha —
`git merge-base --is-ancestor`, exit non-zero on a rewind. A `--force` that
is a fast-forward passes unseen and changes nothing a plain push would not
have. An earlier draft of `prd.md:928-929` asserted the hook refuses
`git push --force origin <branch>`, which no hook can do.

`sd run --sequential <ids>` and `--parallel <ids>` and the two bulk
actions; **the pre-flight validation of a selection**, which no pull request
had: a selection is refused as a unit, before any row is written, when two
selected items resolve to one repository-and-branch pair, and the refusal
names both (`prd.md:1177-1178`, requirement at `prd.md:809-810`). **The
lease does not cover this and a reader will assume it does.** The lease
(`prd.md:803-805`) refuses at *creation*, one row at a time, naming the
holder — so `sd run --parallel a b` on a shared branch would start `a` and
reject `b`, leaving `a` running. The criterion demands the whole selection
refused with nothing created. A lease-only implementation looks right and
fails the assertion. `after` and the delivery barrier, now that PR 5 has
given it something to wait on; the lane rules, including the one that keeps a
parallel row from starting while an eligible exclusive row queued before it
waits; `budget_minutes` at ninety and `budget_usd` only where typed,
accepted on a `url` entry and refused naming a `start` entry. The
runner-owned `pre-push` guard's body, written into the hooks directory PR 1
created, which refuses every push but the item's branch to its own name and
otherwise runs the repository's own hook from `sd.hooksForward`.

**The limit, stated.** `--no-verify` passes the guard. It guards a
session's mistake, not an adversary, and the `prd.md` says so rather than
claiming otherwise.

**Verification.** Criterion 4's batch and chaining assertions: three chained
rows, the second's clone carrying the first's merged change, a conflicting
dispatch merge going `blocked` through `ending` with its clone retained and
no session started, `budget_minutes` timing a row out with its sibling
untouched, and the two `budget_usd` cases. **Plus the selection refusal**
(`prd.md:1177-1178`), which no pull request took: two selected items on one
branch refused as a unit, naming both, with no row written. **Plus the
concurrent-`fetch` assertion** (`prd.md:1178-1181`), which no pull request
took either: two parallel rows in one fixture repository, each provider
running `git fetch -p` and one of them a branch deletion, both ending `done`
with the other's refs untouched. This is the first pull request that can
dispatch two parallel rows into one repository, so it lands no earlier.
**"At the same moment" needs a mechanism this item never named**: both
provider scripts block on a shared FIFO the harness opens, released
together, so the interleaving is produced rather than hoped for. Without it
the test overlaps by luck. Since 2026-09-16 the clause asks for one synchronised run, not twenty; see the section on the landed `prd.md` edits, below, for the
reasoning. **Criterion 1's** two lane tests
at `prd.md:947-956` — two parallel authors in one repository with a serial
row made to wait, and a `merge` row queued behind one running parallel
author with two more parallel rows held off, both starting in queue order
after it. An earlier draft filed both under criterion 4, which contains
neither; its nearest is the `exec`-row lane test at `prd.md:1203-1208`,
which is PR 7's. Criterion 1's pre-push guard and hooks-forwarding block.
Criterion 2's two chained-successor assertions (`prd.md:1077`,
`prd.md:1084-1085`), which the closure table gives this pull request and an
earlier Verification did not take.

## PR 7 — `exec` rows, the hard stops, and what the row records

**Touches:** `local-sd-runner/`.

**`exec` row execution, which no earlier draft scheduled at all.**
Worktree-scope rows run in a clone of their own in the runner's lane
(`prd.md:746-753`); supervisor-scope rows run in the runner's own process,
under the named assignment's lease, making no worktree
(`prd.md:756-761`, `prd.md:803-808`); a read-only entry runs at once and
writes no assignment. Control-scope rows are B's dashboard's and are not
here. Criterion 4 asserts all of it. Without this PR, `sd worktree discard`
from the palette — the documented recovery path for a kept worktree, and
the only one reachable from the iPad — has no executor, and criterion 4
cannot pass with every other PR merged.

**The hard stops, which criterion 3 needs and nothing implemented.**
`prd.md:608-610` requires the runner to stop on the hard stops item A names
— a failing test, a blocking review finding open past the cap, a write
outside the repository — and mark the item `blocked` with the reason. PR 3
*records* the outcome the end carries; it does not decide it. Criterion 3 is
two lines long, which is why it was the easiest in the item to believe was
covered.

**What the row records, and the session's notes.** Criterion 1's opening
sentence asserts the row records provider, start, end, cost and the worktree
path, and that at least one `note` from the session is written; `prd.md:606-607`
makes note ingestion the runner's, for every followup, decision and proposal
the session writes. Cost rows are B's library function, reserved before the
call and settled by call id, which is why this lands after B's slice 4 and
not before.

**The `start` session's cost row is written here.** B's open question 8
settled on 2026-09-05: "the usage read" is the total a `start` session
reports at its own exit, one `run` row per session with the assignment and
the pass on it, and the writer is whatever started the session — this
runner, which execs `claude -p`, `codex exec` or the entry's `start` line
(`prd.md:524`). On a `url` entry the runner writes no cost row at all: B's
library reserved and settled every call, and the row's cost is the sum of
them. The two paths are one branch on the entry's kind, which is why they
sit in one pull request with criterion 1's row record. B's clauses 15.13
and 15.16 are asserted here against a fixture `start` provider; B's PR 8
asserts the row sums like any other. B records it as hand-off 15.

**B's clause 15.7 grep lands here, not there.** B's `2026-09-05-one-database-one-front-door/prd.md:1530-1531` greps
the runner and `sd-review` for a cost insert and expects nothing — every
provider call charged through the one library function and by nothing else.
B credited the whole clause to its PR 8, where the runner half would have
passed for the wrong reason: `local-sd-runner/` does not exist at that merge,
and an empty answer from an absent directory proves nothing. The `sd-review`
half stays with B's PR 8. The runner half is asserted here, where there is a
runner to grep, and it is the natural companion to this pull request's own
rule that the row's cost comes from B's function. B records it as hand-off
13.

**Verification.** Criterion 3's hard stops, which is criterion 3 whole once
PR 3 has landed — PR 3 records the outcome, and this pull request cannot
assert the record without it, so the ordering is what makes the claim true
rather than the pull request's own scope. Criterion 4's palette, `exec` and
supervisor-scope assertions — **not its control-scope assertions**, which
this pull request's own body disclaims: the dashboard acts on control rows
and the runner's half is the ordinary end run, which is PR 3's. Criterion
1's row-record and note clauses, both cost paths included — the `url`
entry's sum over B's rows and the `start` entry's single row from the
session read. **Item B's clauses 15.13 and 15.16**: a `start`-line provider
whose script calls a vendor double three times is charged one `run` row
carrying the session's reported total, with no per-call row, and an
uncapped `start` session that spends past any number runs to its end and is
charged the same way. **Item B's clause 15.7, runner half**: a grep of
`local-sd-runner/` for a cost insert returns nothing — the runner writes a
`run` row through B's library function and inserts none of its own —
asserted where the directory exists.

**Landed 2026-09-12 as PR #278, `feat/runner-hard-stops`.** The first
paragraph was stale by then: `exec` row execution — worktree scope in a
clone, supervisor scope under the lease, the read-only entry — landed in
#227 under a different row (`local-sd-db/sd_db/runner_exec.py`, the `exec`
branch of `Runner._execute`), with provider, vendor, start and end steps and
the worktree path already on the row and the `exec` note written at
release. What this pull request adds is the rest. The hard stops are
`local-sd-runner/sd_runner/hard_stops.py`: a `HardStop` is a
`RunnerRefused` the ordinary ending carries, decided from three sources —
the session's own `.git/sd-stop.json` (the only way the runner can learn of
a write outside the repository, which it does not trace), the repository's
own check run by the runner in the clone after an author exit 0 through a
new supervisor `check` action (`sd-check --json` from the pack; a fixture
passes its own argv), and the `sd-ship` receipt read when `prepare` refuses,
through `sd_db.runner.ship_receipt`. `record_hard_stop` leaves the open
`followup`; the row's `detail`, the `exec` note and that followup carry the
same `hard stop: <kind>: <evidence>` line. Note ingestion is
`sd_runner/session_record.py` reading `.git/sd-notes.jsonl` into
`sd_db.runner.record_session_notes`; the `start` session's cost row is the
same module reading the result envelope `claude --output-format json` prints
last (the author argv gained that flag) into `sd_db.runner.record_session_cost`,
one `run` row keyed `session:<run id>`, idempotent, refusing an unregistered
provider or bill. Tests: `local-sd-runner/tests/test_hard_stops.py` (criterion
3 through the fixture remote: `blocked`, the failure named in the notes, the
branch on the remote; the signal; receipt classification),
`local-sd-runner/tests/test_session_record.py` (the row record and notes;
B's 15.13 and 15.16 with a fixture `start` script calling a vendor double
three times and charged one row with its reported total; B's 15.7 as a
regex over `sd_runner/*.py` and `runner.sh` for cost SQL, and `record_cost`
reachable only through `record_session_cost`), and a `SessionRecord` class
in `local-sd-db/tests/test_runner.py`. Two deviations. The repository check
runs twice on a clean row — once here and again inside `sd-ship prepare`'s
`sd-review` — because the failing test has to be decided before anything
is shipped and `sd-ship` cannot be asked for its check alone; a follow-up
can hand `sd-ship` the runner's result. And the tests do not go through the
pack's `sd_registry`: the fixture path injects the resolved entry
(`provider=`, `check=` seams on `Runner.execute`), so `provider_command`'s
own `start`/`bill`/`reader` fields are covered by inspection, not by a test.
The `budget_usd` gap PR 6 records still waits on B's reservation library —
landed 2026-09-16 in #415 for the creation half; see the Log.

## PR 8 — pulse, sleep, keys, and the service

**Touches:** in `system`, `local-sd-runner/`,
`local-machine-setup/launchagents/local.system-tools.sd-runner.plist` (new),
`local-machine-setup/profiles/personal.agent` and `local-health-check`; in
`sd-ai-command-pack`, `sd runner status`, landing first.

One `runner` row per tick with pid, time and pack version, replacing the
last; `sd runner status` exiting non-zero on a stale or missing heartbeat
and a `local-health-check` check that runs it; `caffeinate -i -w <pid>`
around each session; the plist sourcing `~/.config/shell/env.sh` while a
session inherits only its registry entry's `env` plus `PATH`, `HOME`,
`LANG`, `TERM`, `TMPDIR` and `XDG_CACHE_HOME`.

**The plist follows the house rules the dashboard's already carries.**
`KeepAlive` with `ThrottleInterval` and `ProcessType Background`, both keys
and not one — `local.system-tools.sd-dashboard.plist` records why in a comment.
**The count that comment carries is stale and was mis-copied here.** It
reads "Every other KeepAlive agent on this machine sets both keys, five of
five" — a count of the *other* agents. The dashboard's own compliance
(`:21`, `:24`) cannot falsify a sentence that says "every **other**", and an
earlier draft of this paragraph argued that it did. What makes the count
stale is a sixth other agent: `local.system-tools.second.mcp-obsidian.plist` sets
`KeepAlive` `<false/>` and neither key, so the comment's "five of five" is a
count taken before it existed. An earlier draft of this
paragraph re-quoted it as "five of five KeepAlive agents on this machine".
The checkable fact, and the one this pull request is measured against, is the
repository: `local-machine-setup/launchagents/` holds seven plists that set
`KeepAlive`, six of them `<true/>`, and **all six set both keys**; the
seventh sets it `<false/>`. On the machine itself the two Homebrew-installed
agents, `homebrew.mxcl.herdr` and `homebrew.mxcl.moshi-hook`, set neither key
and are not this repository's to change.

**The entry in `personal.agent` is the point of this PR, not a detail.**
`com.platypeeps.sdw-meter` was in `personal.agent` and not installed, the
commit landed and the machine never got it, and `machine-setup-drift`
reported five items at 03:30 and not that one. `CLAUDE.md` kept that as a
standing lesson under that label until #219 folded it into the rule "A
stage that can remediate must name what it is remediating" — an earlier
draft wrote it `local.system-tools.sdw-meter`, which greps to nothing, in the one
paragraph whose whole subject is a label that was listed and never loaded.
This PR is done when the machine runs the plist and `local-health-check` says so.

**Verification.** Criterion 5's pulse, sleep and key assertions, including
the one that asserts the gap rather than hiding it: a `bash -lc env` from
the same provider script sees the second fixture variable, recorded as the
stated limit and not as a failure.

## Order and dependency

PR 0 before anything, and its findings on `prd.md` before PR 1 opens. Then
1 through 8 in order, each rebased on `main` after its predecessor lands.
PR 5 before PR 6, for the reason PR 5's own rationale gives.

**The pack's `sd` verbs are `sd-ai-command-pack`'s, and three pull requests
add to them.** Every other file on this page is `system`'s, so the Touches
lines said nothing about repositories and three of them were wrong to.
`~/repos/system` has no `bin/` and no `sd` binary; the pack's
`bin/sd` says at line 4 that it "carries four verb groups: `plugin`, `store`,
`config` and `sweep`". So `sd worktree discard|resume|restore` (PR 3),
`sd run` (PR 6) and `sd runner status` (PR 8, required by `prd.md:858` for
`local-health-check`) are each a new verb group in the pack's `bin/sd`, and
each of those three pull requests is a pair: a `system` half and a pack half.
**The pack half lands first in each pair**, because the caller cannot be
written against a verb that does not exist, and the runner's own code is the
caller. Item B settled the mirror-image question for its own code — the
library is `system`'s and the pack only consumes it — and this page never
settled its own.

**Eight dependencies cross an item boundary.** An earlier draft named two,
then six, then said seven over a list of six — the pack's CLI was described
in the paragraph above and never made an entry. It is entry 7 below. Entry 8
is Today, which no round reached.

1. **PR 4 edits `sd-db-backup`, which is B's**, so it lands after B's backup
   path is on `main`.
2. **PR 4's nightly removal runs inside `sd-db-backup`**, after the backup
   has passed (`B/prd.md:1084-1086`, the "Rows and worktrees age" bullet;
   `1081-1082` is the pulse-and-keys bullet an earlier draft landed on) — a
   hook into B's job, not merely an edit to
   a file B owns.
3. **`exec` rows of `control` scope** — kill, quarantine-clear, runner
   restart — are acted on by B's dashboard process and not by the runner at
   all. The runner's side is the ordinary end run it performs on any
   `ending` row, which PR 3 delivers, and B owns the other half. **The two
   items disagree about that half, and this is where it had to be caught.**
   `B/prd.md:789-792` **had** kill "mark the row `blocked` with a `killed by
   operator` note"; those lines now read "writes the row `ending`", so the
   quotation is of the text as it stood, not as it stands.
   `prd.md:763-770` here forbids what it said: the kill
   writes `ending`, "because a row written terminal by the kill would be one
   the start never reconciles, its lease held and its dirty work archived by
   nothing", and criterion 4 asserts `ending` first and `blocked` only after
   the runner's next start. This item's reading is the correct one — it is
   the end-run invariant at `design.md:73-74` — and `B/prd.md` is corrected
   to match. An earlier draft of this paragraph recorded agreement without
   reading the other item's text.
4. **The floor-crossing email goes through B's requirement 9 path**
   (`prd.md:388-390`). PR 4 depends on it.
5. **`budget_usd` is enforced by B's library reserving before every call**
   (`prd.md:823-826`). PR 6's `budget_usd` and criterion 4's budget test
   cannot run without B's reservation, and PR 7's cost rows are the same
   library function.
6. **`sd-ship`'s re-asked safety check** can end a merge row
   `ready_to_send` regardless of `merge_policy` (`design.md:217-220`). That
   is item A's, and PR 5 depends on it.
7. **The `sd` verb groups are the pack's `bin/sd`**, described in the
   paragraph above this list and, until now, left out of it. `sd worktree
   discard|resume|restore` (PR 3), `sd run` (PR 6) and `sd runner status`
   (PR 8) are each a new verb group in `sd-ai-command-pack`, so those three
   pull requests are pairs and the pack half lands first in each. This is
   the one dependency that changes the landing order of three pull requests,
   so it belongs in the list a reader sequences the item from. **Settled
   2026-09-05**: item B raised the same question against its own CLI
   criteria and the operator settled it the way this page already assumed —
   the `sd` verbs are the pack's. B now carries seven pairs of its own. Note
   the halves land in opposite orders: B's `system` half lands first because
   its pack verb calls `sd_db`, and this item's pack half lands first
   because the runner is the caller and the verb is what it calls.
8. **Today is B's screen, and the criteria assert against it ten times.**
   Criterion 1 names a row on Today with its command line, twice, and the
   quarantine; criterion 2 shows a row `done` at once; criterion 5 shows the runner silent; criterion 6
   names the retained-clone removal, names the mid-write
   skip, and names the floor with the free space and the space needed,
   twice. Most render from rows B's Today already draws, and need nothing
   here. **Two do not**: "named on Today with its command line" and "Today
   naming the floor, the free space and the space needed" are new fields on
   a screen B owns, and no pull request's Touches on either page reaches
   them. Recorded rather than scheduled, because which item adds the fields
   is B's landing-order question, not this page's.

## Closing the item

The item closes on the store's row: `docs/work/.status-source` says `row`,
and this `prd.md` carries no `status:` field to move. On 2026-09-16 the
owner accepted the close on sd:235: PR 6's remainder landed in #415 as the
creation-time refusal, and the `budget spent` ending with criterion 4's
two-row test is assigned to sd:234 slice 8b, the runner's half of the reservation library.

That is the whole of it here, and the difference from the pack's items is
worth naming. An item in `sd-ai-command-pack` also drops its `branch:`
field in that edit, because `bin/sd_sweep.py:91` skips an item when
`item.status != SWEEPABLE_STATUS or item.branch`, so a field left behind after
its branch is deleted goes on excluding the item from the staleness sweep —
a gate silenced by the stale metadata it exists to notice. This
repository's items carry no `branch:` field at all: the **two** under
`docs/work/` have `title` and `created` only, status being the row's. So
there is nothing to drop, and no sweep entry is silenced by not dropping
it. The `sd_sweep.py:91` half of this paragraph was checked; the count in
it said three and was not, which is worth recording in a paragraph whose
own claim is that it checked rather than assumed.

## What closes the criteria

| Criterion | Closed by |
|---|---|
| 1 — session in its own clone, row records, note written | PR 1 (clone, hooks, lease), PR 2 (claim ordering, the clean stop draining every claimed child), PR 3 (cleanup, retention, the archive and `sd worktree restore`, the blocked-leader kill point), PR 5 (the `merge` row starting and the merge completing at `prd.md:918-921`), PR 6 (the pre-push guard, hooks forwarding, and the two lane tests at `prd.md:947-956`), PR 7 (the row's records and the session's notes) |
| 2 — `manual` stops at ready, `auto` merges through a merge row | PR 5 (the stop, the merge row, the `auto` path), PR 6 (the two chained-successor assertions at `prd.md:1077` and `prd.md:1084-1085`, which need `after` and the delivery barrier) |
| 3 — a failing test blocks the item, branch left in place | PR 7 (the hard stops), after PR 3, which records the outcome |
| 4 — batches, chaining, lanes, kills, merge barriers, `exec` | PR 1 (the parallel-clone test's `alternates` and byte-identity assertions, and all four starting-commit cases), PR 2 (kills before the end run, the three kill points at `prd.md:1103-1105`), PR 3 (kills needing it — the round-thirty-nine clause at `prd.md:1106-1109` **and the round-forty kill-during-push clause at `prd.md:1110-1116`**, which an earlier table credited by category and no Verification took — the restore and `resume` variants, the squash-merged gate), PR 4 (the scratch paths under `.sd-run/`), PR 5 (merge-row barriers, the `auto` ordering test), PR 6 (batches, chaining, `budget_minutes`, `budget_usd`, **the selection refusal at `prd.md:1177-1178` and the concurrent-`fetch` assertion at `prd.md:1178-1181`**, neither of which any pull request took), PR 7 (palette, `exec`, supervisor scope), PR 3 (the runner's half of control scope: the end run on any `ending` row, so the kill's row stays `ending` while the runner is stopped and its first tick after starting writes the archive, releases the lease and writes `blocked`, and the quarantine lifted on that first tick). **Control scope has no PR 7 half**: PR 7's body says control-scope rows are B's dashboard's, and dependency 3 assigns the runner's side to PR 3. An earlier draft credited PR 7 with both. PR 6 remainder (#415, the creation-time refusal; the `budget spent` ending under sd:234 8b) |
| 5 — pulse, sleep, keys | PR 8 (pulse, sleep, keys, the plist) |
| 6 — retention, restore, the nightly archive, `st_dev` and the storage floors | PR 3 (`st_dev`, `ENOSPC`, every retention, restore, split-index, LFS, rebase and stash assertion at `prd.md:1251-1286`, **and the dated-database-file `restore` clause at `prd.md:1333-1334`**, which sits outside that range and which an earlier table credited by category), PR 4 (both floors, the nightly removal, the nightly re-archive and its skip, the quota-clears-the-floor refusal) |
| 7 — an item with no branch is refused naming it | Landed before the criterion: `_item` in `local-sd-db/sd_db/runner.py` refuses it through readiness, both enqueues and the claim, asserted by `test_an_item_with_no_branch_is_refused_naming_it` in `local-sd-db/tests/test_runner.py` (sd:235, 2026-09-16) |
| 8 — a chain is checked acyclic when it is created | Landed before the criterion: `enqueue` walks every `after` edge before it returns, asserted by `test_a_chain_is_checked_acyclic_when_it_is_created` in `local-sd-db/tests/test_runner.py`; `test_cycle_rejected_before_claim` covers the walk at the claim (sd:235, 2026-09-16) |

Until 2026-09-16 criterion 5 was listed twice here, as two criteria wearing
one number: its title said "Pulse, sleep and keys" and its body, grown
across rounds twenty-six to forty, also carried every retention, restore
and storage assertion in the item. The split is a `prd.md` edit and was
made on the operator's word (sd:235); the rows above read the criteria as
they stand, and the section on the landed edits below says what moved.

**This table was rebuilt clause by clause from the criteria, not patched.**
Round two moved the nightly re-archive into PR 4's body and left criterion
5's row crediting "the nightly archive and its skip" to PR 3, whose Touches
hold no `sd-db-backup` — the exact objection PR 4's own paragraph raises, so
the table contradicted the paragraph written to fix it. Criterion 4's row
omitted PR 1 while criterion 4 asserts PR 1's work four times: the
`alternates` absence and the checkout byte-identity at `prd.md:1132-1135`,
and the three starting-commit cases at `prd.md:1140-1151`. Criterion 1's row
gave `prd.md:951-956` to PR 5, whose Verification never took it, while PR 6's
Verification took it under criterion 4's number. Each earlier patch was right
about the clause it named and wrong about a neighbour, which is why the whole
table was re-derived this time.

**No criterion is claimed "whole" by a pull request that cannot reach all of
it.** An earlier draft gave criterion 4 whole to the batches pull request,
which held roughly half of it, and gave criterion 1 to three pull requests
when the rebuilt row needs six — PR 1, PR 2, PR 3, PR 5, PR 6 and PR 7. An
earlier draft of this sentence said five, counting the table as it stood
before the rebuild. Both are corrected above, and the correction is why the
item is eight pull requests rather than seven.

## The `prd.md` edits this page reserved, landed 2026-09-16

Until 2026-09-16 this section was titled "A `prd.md` defect this page
cannot fix" and reserved three criteria edits for the operator, who
approved all three on sd:235 ("2.1 yes, 2.2 yes, 2.3 yes"). They landed in
this pull request, one docs change with the two new criteria's tests beside
it. What each said, and what is true now:

**The storage routing, and the hook.** Three sentences routed the storage
assertions to criterion 4 and a fourth mis-routed outside storage. Criterion
4 spans `prd.md:1095-1230` and contains no floor, `statvfs`, volume or
device assertion; every one of them was in criterion 5, at
`prd.md:1299-1301` for the first device refusal and `prd.md:1335-1360` for
the rest, and the `.husky/_` hook is criterion 1's, at `prd.md:982-991`.
Criterion 5 is now split at its retention sentence: criterion 5 is pulse,
sleep and keys (`prd.md:1231-1243`) and criterion 6 is retention, restore
and storage (`prd.md:1244-1364`); nothing in either body moved, so the
citations into them above still hold. The three storage sentences name
criterion 6 now — `prd.md:1865` ("Criterion 6 runs under a `statvfs`
double"), `prd.md:2029` ("criterion 6 says the same") and `prd.md:2042`
("Criterion 6 reduced to the floor, the full volume, the device rule and
the scratch paths") — and the hook sentence names criterion 1
(`prd.md:1972`). The consequence C-49 named stands as it did: the "ends
every running row" behaviour of the database volume's floor has no
criterion, in criterion 6 or anywhere; the sentence at `prd.md:2027-2029`
still states it and no test is asked for. That is a further `prd.md` edit,
not this one.

**Measured while making it, and left as written**: `grep -n "criterion 4"`
over the log finds more routing sentences than the four the operator
approved. Rounds nineteen to thirty-one route retention and restore
assertions that now sit in criterion 6 to criterion 4 — `prd.md:1682`,
`prd.md:1691`, `prd.md:1699`, `prd.md:1712`, `prd.md:1721`, `prd.md:1764`,
`prd.md:1778`, `prd.md:1789`, `prd.md:1799`, `prd.md:1819`, `prd.md:1834`,
`prd.md:1852` and, for two rows ending on a full volume, `prd.md:1942`;
rounds thirty-three to thirty-nine route storage assertions that the
consolidation after round forty reduced away — `prd.md:1887`,
`prd.md:1903`, `prd.md:1918`, `prd.md:1928`, `prd.md:1962`, `prd.md:1984`
and `prd.md:2010`, each naming a case the criteria no longer hold; and two
more hook-fixture sentences read like the one that was fixed, `prd.md:1874`
and `prd.md:1950`. None of the three groups is in the approved edit. They
are recorded here by line so the next edit does not re-measure them.

**The two unasserted sentences are criteria 7 and 8.** `prd.md:811`
requires that "an item with no branch is refused naming it" and
`prd.md:716` that "A chain is checked acyclic when it is created"; both
were behaviours the design committed to and no criterion asked a test for.
Both behaviours already existed — sd:438's note 670 of 2026-09-11 records that
"enqueue refuses a NULL branch for every role including exec", which is
`_item` in `local-sd-db/sd_db/runner.py`, and `enqueue` there walks every
`after` edge through `_acyclic` before it returns — and now each has a
criterion, `prd.md:1365-1372` and `prd.md:1373-1381`, and a test in
`local-sd-db/tests/test_runner.py` that dies on a byte-copy mutation of its
refusal: `test_an_item_with_no_branch_is_refused_naming_it` fails with
`(True, None) != (False, 'item 5 needs a valid branch')` when `not branch`
leaves `_item`, and `test_a_chain_is_checked_acyclic_when_it_is_created`
fails with `RunnerRefused not raised` when the `_acyclic` call leaves
`enqueue`. The claim-time walk was already covered by
`test_cycle_rejected_before_claim`; the creation-time walk was not.

**The requirement and the criterion disagree about what the dated database
file names.** `prd.md:467-468` says it may name "an **archive** the nightly
job has since pruned"; `prd.md:1333-1334` says it names "a retained
**clone** the job has removed". The criterion is the one that is right:
`prd.md:1293` asserts "a kept worktree's archive untouched" at both twenty-
nine and thirty-one days, so the archive is not what that job prunes. The
requirement's premise contradicts the retention rule its own criterion
asserts four lines earlier. PR 3 is written to the criterion. Not in the
approved edit; still open as a `prd.md` edit.

**The concurrent-`fetch` clause is one synchronised run now, not twenty.**
`prd.md:1178-1181` asked for twenty runs, from D's round six. The race it
targets was real when parallel authors ran in worktrees of one checkout
sharing `.git` — that is C-7's own statement of it. The fix removed the
sharing: `--dissociate`, no `alternates`, each clone owning its refs and
objects (C-13). After that the two clones write to disjoint `.git`
directories, so the outcome is deterministic and twenty runs sampled one
point twenty times. Nor would a regression resurface there: `alternates`
shares objects and never refs, so even a clone that kept the alternate
could not let one row's branch deletion reach another's refs. The only
regression that reintroduces C-7 is reverting to `git worktree add`, and
criterion 4 already catches that deterministically in one run — by the
byte-identity assertions at `prd.md:1134-1136` and `prd.md:1175-1177`, not
by the `alternates`-absent one beside them: a linked worktree's `.git` is a
file with no `objects/info/alternates` either, so that check passes the
regression, while its `gc --prune=now` repacks the checkout's store and its
`fetch -p` writes the checkout's remote-tracking refs, which the byte
comparison sees (#412's review). The clause now asks for that one run,
released at the FIFO rendezvous PR 6 names, and carries the reasoning in
its own words. The `budget_usd` clause beside it is not this edit's: its
creation half landed in #415 and its `budget spent` half is assigned to
sd:234 slice 8b (the Log below).
## Open, and not blocking

The work volume's size. The `prd.md` names `<size>` as a placeholder the
operator fills at setup and fixes no number; `free_floor_gb` forty is the
only figure written. PR 4 needs the volume to exist, needs its size to
exceed the floor, and does not care what it is beyond that.

**The tracked `local.system-tools.sd-runner.plist` is the installed one now
(2026-09-12).** `machine-setup.sh status` reported it `DIFFERS` from #227
on: the repository held a hand-written minimal plist, and the machine held
what `runner.sh install-plan --config ~/.config/sd/runner.json` renders —
plistlib formatting, `--config` on `ProgramArguments`, and
`SD_RUNNER_PYTHON` in `EnvironmentVariables`. The installed copy is the
correct one, for the same reason `local.system-tools.sd-dashboard.plist` was repointed
to its installer's exact bytes: a tracked plist that `install-plan` does not
render reads `DIFFERS` after every install. Captured byte-for-byte (`diff`
against `install-plan`'s output is empty), no PR 8 work behind it. The
caveat the tracked plist already carried stands: `~/...` and
`/Volumes/sd-work` in `runner.json` are this machine's, and a second
`personal` machine writes its own.

## Log

- **2026-09-16** — The item closes on sd:235. PR 6's `budget_usd` remainder
  landed in #415 for the creation half: `source:local-sd-db/sd_db/runner.py::enqueue`
  takes `budget_usd`, and the ledger's entry rule refuses a `start` author
  naming the entry before any row is written, asserted by
  `source:local-sd-db/tests/test_runner_budget.py::test_a_budget_on_a_start_author_is_refused_at_creation_naming_the_entry`
  and
  `source:local-sd-db/tests/test_runner_budget.py::test_no_budget_writes_null_and_a_budget_on_a_url_author_is_stored`,
  with the dashboard's field asserted by
  `source:local-project-dashboard/tests/test_run_budget.py::test_both_run_dialogs_offer_a_budget_usd_and_the_refusal_reaches_the_page`.
  The other half, a `url` row ending `blocked` with `budget spent` and
  criterion 4's two-row test, moved to sd:234 slice 8b by the owner's
  decision, because the runner executes only `start` entries: the dispatch
  loop skips at `if not provider.start:` in
  `local-sd-runner/sd_runner/runtime.py`, and `local-sd-runner/sd_runner` holds no path that
  executes a `url` author. The criterion 4 clause in `prd.md`, the row in
  the table above and the PR 7 sentence that waited on the library carry
  the pointer.
- **2026-09-16** — #416 merged as 3f343054 on main; the item is delivered.
