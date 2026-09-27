# local-sd-plan

Planning documents get written for work that is only a row.

A change with a shape worth agreeing on first gets a folder under
`docs/work/` — `prd.md`, `design.md`, `implement.md`. Nothing starts one. The
folder appears when a person opens a session and types `/sd-plan <slug>`,
which means the documents exist for work somebody happened to be sitting in
front of. This folder is the trigger: one from the dashboard, one from cron,
and one executor underneath both.

## Usage

```sh
./sd-plan.sh item 438   # write one row's planning documents, here, on its branch
./sd-plan.sh item 438 --dry-run   # say what that would do, write nothing
./sd-plan.sh nightly --dry-run    # what tonight would plan, enqueueing nothing
./sd-plan.sh nightly    # select one row per participating repository, enqueue each
./sd-plan.sh status   # is this machine set up to plan anything, as an exit code
./sd-plan.sh test     # this folder's suite, run by system-native CI
./sd-plan.sh help     # the subcommands
```

The plan behind all of this is
`docs/work/archive/2026-09/2026-09-11-planning-documents-get-a-trigger/`.

## `item <id>` writes the documents, and does its own git

Run it from inside the checkout the row belongs to. It reads the row, refuses
one whose `repo` is not this checkout (R10-D6, checked rather than trusted),
derives `docs/work/<created>-<slug>/` from the row's date and title, runs
`claude -p "/sd-plan <slug> --from sd:<id> ..."` headless (the prompt ends in a sentence that says the run is unattended and names the documents to leave: `prd.md`, `design.md` and `implement.md`, or the narrower set the Jev judgment below chose), and then verifies that
all three documents exist before it commits anything -- all three unless the optional judgment in "Jev can say which passes an item needs" narrowed the list. A run that wrote two of
them fails, loudly, with the folder left in place to look at.

**It commits and pushes itself, and that is not an accident of convenience.**
The obvious division of labour — the command writes, the runner delivers — is
not available here. Enqueue refuses any row whose `branch` is NULL for every
role including `exec` (`local-sd-db/sd_db/runner.py:53`), so the branch has to
be on the row before it is ever queued; and the exec path then skips the item
transition entirely (`local-sd-db/sd_db/runner.py:333`) while the clone's
`pre-push` hook refuses any push off the leased branch
(`local-sd-runner/sd_runner/gitops.py:160`). So the command owns the whole
path from row to pushed branch. The payoff is that it behaves identically by
hand in a working tree and under the runner in a clone: it wants a checkout
and nothing else.

It refuses before touching anything on a dirty tree, on a `done` or
`ready_to_send` row, and on a row whose `created_at` will not yield a date. A
folder that is committed and has its row is reported and left alone, exit 0 —
a second trigger is a no-op, not a second folder.

The registration step is the pack's `sd work register`, run as
`$SD_PACK_ROOT/bin/sd` under the interpreter `sd-plan.sh` pinned rather than
under `bin/sd`'s shebang. `SD_PACK_ROOT` defaults to
`~/repos/platypeeps/sd-ai-command-pack`, the path `local-bin-links` and the
cron jobs already assume. A pack that is not there is refused before the
planning run starts, naming the path and the variable. Standing on
`plan/<slug>`, the verb records that as the row's branch (pack sd:621).

Registration comes before the commit. A refusal from `sd` fails the run with
`sd`'s exit code and its stderr in the message, and leaves the documents on
disk, uncommitted. That is what keeps a folder without a row off the remote:
`item` pushes nothing, and the runner, finding uncommitted work in the clone,
archives the clone rather than pushing its branch
(`local-sd-runner/sd_runner/runtime.py:650-655`). A branch with nothing new
committed has nothing to publish. Running `item` again finishes the job: a
folder without its row is not "already planned", so the retry registers it,
commits and pushes, without a second planning run. Uncommitted changes are
still refused anywhere except under that item's own folder.

The commit names that folder as its pathspec rather than taking the index.
The agent runs after the dirty-tree check, so a file it staged would otherwise
ride the documents commit and be pushed with it; now it stays staged, outside
the commit, and the clone is kept as dirty.

`sd_db` is whatever that interpreter has installed: the pack's provisioned
virtualenv on a machine, CI's venv in CI. Nothing puts `local-sd-db` on
`PYTHONPATH` for `item` any more, and an interpreter without the library is
refused in a sentence.

## The button, and the branch it needs first

The dashboard's Commands dialog runs `item` through the runner. The entry is
in `local-sd-runner/commands.example.yaml` as `plan-item`; catalogs are
machine state and nothing installs one, so copy it into
`~/.local/share/sd/commands.yaml` and **change `argv[0]` to this checkout's
absolute path** — it is hashed into `entry_sha256`, and an entry whose
`argv[0]` is relative, unreadable or not executable is dropped from the
catalog without a word. Check it was taken with:

    sd runner commands catalog --screen item --item <id>

**A row needs a branch before the button will do anything.** Enqueue refuses a
NULL branch for every role, `exec` among them, and a row from
`sd task add --here` has none — the button answers
`commands: item <id> needs a valid branch`, which is a database refusal and not
a dialog validation error, so it is worth recognising. The supported remedy is

    sd runner prepare <id> --branch plan/<slug>

run from inside the repository the row belongs to. It sets the row's `repo` from the current checkout and its `branch`
from the flag, and changes no git ref or working tree; it does ask git first (`check-ref-format`, `rev-parse`, and
`ls-remote` against the remote), so the remote must be reachable. `item` then uses that branch
rather than deriving one, which is why a run under the runner and a run by
hand land on the same branch.

`mutates: true` and `scope: worktree` put the command in the runner's queue
rather than running it in the dashboard's process, so the dialog returns a
queued assignment immediately and the output is read back from
`/api/executions/<note>`. A mutating worktree command leaves the item's status
alone (`local-sd-runner/COMMANDS.md:22`) — planning does not ship anything, and
the row stays where it was.

## `nightly` selects, and the runner does the work

One row per participating repository, every night at 04:45 via
`local-cron-jobs`' `sd-plan-nightly` job. It is `JOB_COMMAND` and not
`JOB_PROMPT` on purpose: selection is deterministic and belongs in cron, while
the agent belongs in the runner's isolated clone where it is bounded, logged
and resumable.

Its `sd_db` is the one `local-sd-db/sd-db.sh library` names (sd:1812). That
is this checkout's `local-sd-db` on `PYTHONPATH`, unless PYTHON has a copy
installed that is built for a newer schema: a primary checkout can sit a
migration behind the live database, and the night would refuse. The switch
prints one line on stderr, and `SD_DB_LIBRARY` forces a side, as it does for
`sd-db.sh` itself. `test` sets `checkout`, so the suite imports this checkout.

Selection asks the database, never the filesystem: `repos.registered()` reads
the `repo` table for the enumeration, and `backlog_items` returns unparked open
rows already ordered by priority, then due date, then id
(source:local-sd-db/sd_db/reads.py::backlog_items) — so "best" needs no opinion
here. A row is skipped if its folder already exists, if its date will not
parse, or if its title yields no slug the lint could read. Each selected row
gets a branch through `configure_item` and is then enqueued with the same
`plan-item` entry the button uses. A row already on a `plan/` branch goes through
`configure_item` again every night, on the branch it has, because it also
saves the default branch's head and the runner refuses a new branch whose
saved base has moved. Without that, a night whose enqueue refused left a stale
base behind, and the row's next run ended blocked once the default branch
moved (sd:778). The branch keeps its name: a title edited since then yields
another slug, and a `plan/` branch set by hand is the operator's. A row on any
other branch was set up by hand, and the nightly leaves its setup alone.

A night that refuses for the whole repository sets no row up either.
`runner_exec.standing_refusal` enumerates every refusal `prepare` makes alike
for every row, and the night asks it once per repository: a restore
still to be reimported (sd:786), a command palette that changed since the night read it, `plan-item` registered on some other screen
(sd:805) or rejected from the catalog, typed values that are not the names
or the kinds its entry declares, and a `plan-item` that is not a mutating
worktree command (sd:814). A palette that cannot be read at all is not among them: the night reads it with
`runner_exec.catalog()` before it asks, and that failure fails the whole night. The night only queues, and `prepare` runs such an
entry at once instead of queuing it, so the night asks for a queue and is
refused rather than setting the row up, writing an exec note and saying it
queued a row that never runs.
`configure_item` writes a decision note and bumps the item's revision on every
row whose base moved, so before this a refusal that stood for three nights
wrote that three times over on nights that queued nothing. Those refusals are
not the ones that read nothing: each queries the store for a pending restore,
and all but that one read the palette file. What none of them reads is the row.
Every other refusal either reads the row or resolves one of its values against
the store or the filesystem — a stale row, a row with no registered repository,
a provider that is not enabled, a destination already on disk, a NULL branch,
an assignment already open, a retained lease — and each of those is one row's
own turn, so the night sets the next row up and tries it (sd:820). The answer is
read on the first row the night tries, which that row's own refusal may still
turn away, and it can go stale before a later row's turn.

A planned row is still `planning`, and its folder is on `plan/<slug>`, not in
this checkout, until that branch merges and the checkout pulls. So a row is
also skipped when its folder already has its work row, when an assignment
holds it (queued, running, `ending` as a kept run stays, or `blocked`), or
when the branch it would run on is already on the repository's remote. That is
the branch the row has, and `plan/<slug>` only when it has none: a row renamed
after its setup keeps the branch it was set up on, and asking about the slug
the new title yields queued it again every night (sd:793). The remote is asked
last, and only about a row that passed every database check. A remote that
cannot be asked there is named on stderr and the check lets the row through.
Setup then asks the remote again and refuses a row with no branch or a `plan/`
branch when it cannot, so that row is named on stderr and not queued. Only a
row on a branch set by hand outside `plan/` is queued anyway. Without these
checks the nightly queued the same top row every night, and the row behind it
was never planned (sd:774).

A blocked run is not the nightly's to retry unattended, and it cannot be
requeued: `runner.requeue` refuses every exec assignment, because an execution
authorization is single-use. To recover, plan it again from the item's button:
under "Run with an agent", "Save run setup", then "Write the planning
documents" (`plan-item`) in the Commands dialog queues a new run. "Save run
setup" saves the default branch's current head only while the branch is still
new. A run that blocked after the runner's branch step has already pushed
`plan/<slug>` to the remote (`git(root, "push"`, in `branch` of
`local-sd-runner/sd_runner/gitops.py`), so
setup saves no base, and the new run continues from that remote branch.
Until a newer run is on the row, the nightly names it on stderr every night it
passes it over, so a backlog drained by blocked runs does not read as "nothing
to plan tonight". The newest assignment decides that, not the blocked one: a
recovery run that has finished is no longer queued, running or `ending`, and
naming the row after that told the operator to plan again what they already
planned again (sd:793).

**One per repository, not two.** The original ask was two; across twelve
registered repositories that is twenty-four unattended planning runs a night
against an assumption nobody has measured once. The number is
`PER_REPOSITORY` in `sd_plan.py`, in one place, and a test pins that it is in
only one place. It counts rows queued, not rows tried: a row the queue refuses
is named, and the next row in that repository gets its turn the same night.

It exits 0 when no repository participates, when no participant has a
candidate row, and when every row it tried was queued or refused on its own
account — an absent or empty participation list is the ordinary state, not a
fault. It exits non-zero when it could not ask (no runner dispatching, no
database, no readable palette) and when a standing refusal skipped a
repository, below. That last distinction is
deliberate and tested: a row the queue refuses on its own account is named and
the night carries on, while a palette that cannot be read fails the night,
because otherwise a machine with nothing configured would report a successful
night of doing nothing.

**A repository skipped for the night fails the night too** (sd:842). A
standing refusal stops a repository where it is read, and until this the only
trace was one stderr line — which under launchd is a line in
`local-cron-jobs/logs/sd-plan-nightly.log` that nobody opens. The night still
exited 0 and still printed "nothing to plan tonight", so a nightly that had
stopped planning a repository read exactly like one that ran and found
nothing. That is the shape sd:766 filed once already, for the nightly never
enqueuing at all.

The exit status is the whole signal, and nothing here writes it anywhere:
`cron-jobs.sh`'s `cmd_exec` already branches on the rc it gets back, and the
three surfaces this item weighed all hang off that one branch.

- **A push, the same night.** `notify_failure` writes `logs/failures.log`,
  raises a macOS notification, and pushes to ntfy when `notify.conf` sets a
  topic. `watchdog-daily` reads the same log.
- **A followup row on Today.** `cmd_exec` ends every run with
  `record_run_report`, which is `sd reports ingest --exit-code`. In
  `sd_db.reporting.ingest_log`, `attention = exit_code != 0`, and an
  attention report opens an item `sd-plan-nightly: needs attention` carrying
  a `followup` note — *"Review sd-plan-nightly findings from …; job exited
  1."* — which is exactly what `reads.open_followups` puts on **`/today`**,
  oldest first, until a person resolves it. A clean night records a heartbeat
  and no item at all, so the row appears only when something is wrong.
- **The run report itself.** The same item is a report, with the night's
  stderr as its body, on **`/operations` → Reports**; `is_urgent` promotes an
  attention report into the matrix's urgent quadrant by name.
- **The dashboard's Jobs area.** **`/operations` → Jobs** reads launchd's
  `last exit code` through `sd_db.operations.LaunchdBackend._parse`, which
  maps any non-zero one to `failed`. `sd-plan-nightly` sits there red, with
  Retry enabled, until a night succeeds.

So the choice was not between the three destinations the item offered. It was
to set the one field that is upstream of all three, rather than write new
state on a night whose whole design is to write nothing when it refuses —
which is the point of `standing_refusal` (sd:786, sd:805, sd:820). A followup
written from here would also have had to hang on an item, and would have read
as that row's fault when the refusal is the repository's; the one the report
opens hangs on the report.

The night is non-zero even when another repository queued a row. The failure
is one repository of the fleet quietly ceasing to be planned; a night that
only failed when it queued nothing at all would stay green forever while that
one was never planned again.

**A repository with no candidate is never asked, and the night is green**
(sd:877). The standing question is put on the first row a real night tries. A
repository whose every row is passed over — already planned, held by an
assignment, its branch already on the remote — puts no question at all, so it
prints "nothing to plan tonight" and exits 0 even while a standing refusal
stands on the machine. That looks like the sd:842 hole and is not one. A
refusal stops the repository before the first enqueue, so it writes nothing
and passes no row over: every row skipped that night was skipped for a reason
of its own, and nothing is waiting on the refusal. The first night a row *is*
a candidate, a real night puts the question and goes red — the first night the
refusal costs anything.

A *real* night, because `--dry-run` asks nothing at all, for any repository,
and is green however many candidates it reports: it reports each one and moves
on before the palette is read, so it never reaches the question. That is the
rule's premise rather than an exception to it. A dry run enqueues nothing, so
there is no setup for the standing answer to guard, and it is an operator
reading what a night would select rather than the run launchd watches. Every
sentence here about what the night exits describes a real one.

Asking ahead of the loop was weighed and rejected. It needs the palette, which
`standing_refusal` reads before every refusal but the pending restore, and
reading the palette on a night that would queue nothing fails every quiet
night on a machine with nothing configured — a machine behaving correctly.
That false red recurs nightly and forever, against one night of earlier
warning, once, when the backlog stops being empty. It also needs a row, since
the question type-checks the `item` placeholder it is asked with, so a night
with no candidate would have to invent an id and ask something no real
request will ever ask. Asking only the pending restore — the one refusal
needing neither palette nor row — was rejected too: it is a second copy of a
check kept in one place so a caller cannot drift from what the real request
does (sd:820).

    ./sd-plan.sh nightly --dry-run   # select and report, enqueue nothing

`--dry-run` is the safe way to see what a night would do. A real run enqueues,
and on a machine with a live runner an enqueue starts an agent.

## `status` answers with an exit code

Convention 6, and the distinction it turns on is the one that decides whether
a machine raises a nightly finding forever:

| Code | Means | When |
| --- | --- | --- |
| `0` | healthy | repositories participate and the runner is dispatching |
| `3` | nothing to check | no participation list on this machine, or one naming nothing |
| `1` | up and actually broken | repositories participate and the runner would not run them |

`3` is a machine that was never set up for this, and it must stay silent:
`local-health-check` reports only `1`, so a tool whose `status` cannot tell
"unconfigured" from "broken" makes that check lie.

The runner's own `3` stays `1` here (sd:1387). `sd-runner status` exits `3`
when launchd does not hold its agent, which is its "nothing to check". For
this tool it is not: a booted-out agent reads the same, and `nightly` refuses
to enqueue in that state. Passing `3` through would silence the check while
the night still fails. The finding's first line names the cause instead:
"the runner's agent is not loaded". Load the agent, or empty the
participation list if the machine should not plan.

`1` is the specific failure worth waking somebody for — a participation list
that says to plan, and a runner that would not execute what gets enqueued.
Without this, `nightly` would enqueue into a queue nobody drains, exit 0, and
report nothing, every night.

### `nightly` asks the two questions in order

`nightly` reads the participation list **before** it probes the runner. Both
orders look the same on a configured machine; they differ on every other one.
The other order shipped, and 2026-09-20 is what it cost: this machine has
never had a `repos.personal.conf`, the runner was not up at 04:45, and a night
that was going to enqueue nothing logged `FAILED rc=1` against a runner it had
no work for (sd:1202). The runner is asked only where its answer can change
what happens, which is only when some repository participates.

The exit codes are unchanged: `0` when nothing participates or every selected
row was queued, non-zero when the runner would not dispatch, a repository was
skipped, or the palette or database could not be read.

A stopped runner and a runner that refuses to dispatch both read as `1`, and
that is deliberate rather than unfinished. `cron-jobs.sh` notifies on every
non-zero code and `reporting.py` marks every non-zero code for attention, so a
third code for "nothing to check" would still page, and the page would still
say `FAILED`. Giving a stopped runner its own code is a change to those two
consumers first, and this branch does not make it.

## Participation is a list, and an absent list means nobody

`repos.<profile>.conf` beside the script, one absolute repository path per
line, `#` comments and blank lines ignored. The profile is the one
`machine-setup` recorded in `~/.config/machine-setup/profile`, overridable
with `SD_PLAN_PROFILE`; the same resolution `local-repo-sync` uses, for the
same reason it documents at length. Unlike `local-repo-sync`, no recorded
profile means no list, not `personal`'s: `status` exits 3 and `nightly`
queues nothing. A path listed twice participates once. A list or profile that
exists but cannot be read is a broken machine: `status` and `nightly` exit 1.

**The file is local and gitignored, and opting a repository in is deliberate.**
It names the operator's own repositories, so it never ships. What it opts in
to is an agent writing a branch in that checkout, unattended, nightly, with
nobody watching. `repos.personal.conf.example` ships entirely commented
out, and a test asserts it stays that way: an example that participates when
copied opts a repository in by being copied.

A path must be one the `repo` table already holds (`sd-db.sh repo list` is the
enumeration). A path that is not registered names nothing.

## Why the runner, and not a cron job that runs an agent

The obvious build is a `JOB_PROMPT` job telling an agent to pick items and
plan them. `dependabot-daily` is exactly that and it works. Here it is the
wrong shape, and the fourth reason is the decisive one:

- it bounds nothing — no `JOB_PROMPT` job in the fleet sets a runtime budget;
- it isolates nothing — it runs in `JOB_DIR`, which is a primary checkout that
  other sessions are working in;
- its output is a log nobody reads;
- it would be a second executor. A dashboard button cannot be a cron job, so
  the palette path is taken regardless, and building the job on a different
  path means two things that write planning documents and only one of them is
  the one anybody debugs.

The palette path gives all four away for free: `runner_exec.prepare()`
enqueues and returns, the runner clones the repository, runs the command
inside that clone under a 24-hour ceiling, and streams output to a log the
dashboard already serves.

## Jev can say which passes an item needs, unless the stage is switched off

Not every item needs three documents. A one-line change with an obvious check
does not need a build sequenced in advance, and a change whose shape the
requirements already fix does not need a design pass to fix it again. Today
every planned item gets all three anyway, because nothing was deciding.

`local-jev` decides unless `JEV_SD_PLAN` switches this stage off. The run then
writes `prd.md` on its own first, asks Jev two questions about that prd in one
`ask` request, and asks the agent a second time for whatever the answers kept:

```sh
./sd-plan.sh item 402
sd-plan: 2026-09-20-a-shape-worth-agreeing-on plans prd.md, implement.md \
    -- Jev: design 0.10 (skipped), implement 0.92 (planned)
```

**Two questions, not one.** A design pass and an implement pass are two
decisions, and an item can plainly need the second without the first -- a
small, obvious change that still lands in six steps. They are two `noul`s in
one request, which is what makes them run in parallel and cost about what one
costs; they cannot see each other's answers, and neither needs to, because
both are about the same prd. The gate is `JEV_GATE`, deliberately under a
half: an unwanted document costs a read, and a missing one costs the pass
nobody made.

**The prd comes first because the prd is the question.** The judgment is about
what the written requirements already settle, so it cannot be made before they
are written -- which is why this splits one planning run into two. That
is its price: when both passes are needed, this asks the agent twice to reach
the same three documents a default run reaches once. When a pass is dropped,
it buys back a document nobody had to write or read.

**Nothing depends on it.** `JEV_SD_PLAN` only ever subtracts -- it can switch
this stage off, never switch a reading on that `jev enabled` would decline --
and it is settled before the prd is written rather than between the two runs.
A switched-off `JEV_SD_PLAN`, a `jev off` machine, a machine with no `TYPESAFE_API_KEY`, a
call that fails or times out, and an answer this cannot parse all end the same
way: all three documents, the prompt the agent always got, and the reason on
stderr. That is the rule in `CLAUDE.md`'s "Nothing may depend on Jev", and
`tests/test_jev.py` is a case for each of those ways.

Jev is reached at `../local-jev/jev.sh`, resolved from this folder, not as
`jev` on `PATH`: the runner executes this with a bare `PATH`, the same
environment that once made `python3` resolve to Xcode's 3.9 and `claude`
resolve to nothing. `SD_PLAN_JEV` replaces that argv, which is how the suite
answers for Jev offline, and `SD_PLAN_DOCUMENTS` carries the narrowed list to
an `SD_PLAN_AGENT` double.

**A prd-only item is a legal item.** `sd-docs-lint` fails an item without a
`prd.md`, and fails a file in the folder that is not one of the three, but no
rule asks for `design.md` or `implement.md` -- rule 2 checks acceptance
criteria, open `BLOCKING:` lines and a recorded branch. Measured before this
was built, with a throwaway prd-only item under `docs/work`:
`rules 1-2 work items: checked 8 item(s), 0 of them workable by rule 2`, and
then `sd-docs-lint: clean`. So a skipped pass leaves the gate green, and the
run skips the document rather than writing one to keep a linter quiet.

**The judgment outlives the run.** It is printed, and when Jev answered it is
the last paragraph of the documents commit --
`Jev: design 0.10 (skipped), implement 0.92 (planned)` -- because a folder
with no `design.md` is a claim somebody will want the reason for, and stdout
is a log nobody keeps. The numbers are kept and not only the verdict, so a
reader who disagrees can see how close the call was. Nothing is written into
the prd: the file the judgment read is the file the planning run wrote, in
every case.

### What leaves the machine

Every Jev call leaves the machine, so this is the whole list: **the text of
`prd.md`**, as the `--state` of one request, and the two question
instructions, which are fixed prose in `sd_plan.py` and name no repository.
Nothing else -- no row, no title, no item id, no branch name, no repository
path, no other file in the folder, no credential. `jev enabled` sends nothing
at all, which is what makes it free to ask.

A prd is internal planning prose, and some of it is not for a third party.
**Set `JEV_SD_PLAN=0` when planning something confidential.** Unset means on,
so a prd written with nothing set is a prd that leaves the machine; keeping it
here is an action you take. `jev off` and an unkeyed machine stop it too.

## Tests

`./sd-plan.sh test`, and `system-native` runs it in CI. Python rather than
shell for the reason `local-repo-sync/README.md:90` gives: the CI wrapper
asserts a `Ran N tests` summary and fails on any skip, and a shell harness
produces neither.

The runner is doubled by a four-line script answering `status` with the two
JSON fields this one reads. The suite never needs a runner installed, and the
four ways a runner can fail to dispatch — unhealthy, dispatch disallowed, not
installed, answering with something that is not JSON — are four cases rather
than one.

One case reads this folder rather than a fixture: `TheExampleList` asserts the
shipped example names no live repository and that no `repos.*.conf` is
committed. It fails the moment somebody commits one: the list is private
configuration.

`TheConventions` reads the verb list out of the script's own `case` rather
than a list written beside it. A list written beside it is one more inventory
to remember: `local-sd-db/tests/test_cli.py` carried one that named ten verbs
and agreed with itself while an eleventh was dispatched and documented.
