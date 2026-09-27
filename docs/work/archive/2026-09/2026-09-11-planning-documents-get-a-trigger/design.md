# Design — planning documents get a trigger

The shape is one executor with two triggers, and the executor is not new
machinery: it is a registered palette command, which means the runner already
provides the isolation, the bound and the log.

## 1. Why the runner queue and not a job that runs an agent

The obvious build is a cron job whose `JOB_PROMPT` tells an agent to pick
items and plan them. `dependabot-daily` is exactly that and it works. It is
the wrong shape here for four reasons, and the fourth is the decisive one.

- **It bounds nothing.** No `JOB_PROMPT` job in the fleet sets a runtime or
  token budget; `No overlapping runs of the same job`, in
  `local-cron-jobs/cron-jobs.sh`, gives them an overlap lock and nothing
  else. A run that hangs blocks its own next firing and is never killed.
- **It isolates nothing.** The job runs in `JOB_DIR`. Writing a branch into a
  primary checkout is the thing `CLAUDE.md`'s "Three sessions share this
  checkout" section exists to forbid.
- **Its output is a log nobody reads.** The runner writes each execution to
  `<db-dir>/executions/<uuid>.log`, which the dashboard already serves at
  `/api/executions/<note>`.
- **It would be a second executor.** The dashboard button cannot be a cron
  job, so the button would take the palette path regardless. Building the job
  on a different path means two things that produce planning documents, and
  requirement 3 says there is one.

The palette path gives all four for free. `runner_exec.prepare()`
(`local-sd-db/sd_db/runner_exec.py:212`) writes a write-ahead `exec` note,
enqueues a runner assignment for a `mutates: true, scope: worktree` entry, and
returns. The runner claims it, clones the repository
(`local-sd-runner/sd_runner/gitops.py:49`, `--reference` + `--dissociate`, so
the clone is cheap and independent), runs the command inside that clone under
a 90-minute assignment budget (`budget_minutes=90`, in `enqueue` of `local-sd-db/sd_db/runner.py`; corrected 2026-09-17: until then this line cited the 86400-second ceiling of `local-sd-runner/sd_runner/exec_protocol.py:11`, which is the subprocess timeout beneath the budget and never binds first), and
streams output to the durable log.

**The `exec` role does not move the item's status.**
`local-sd-db/sd_db/runner.py:191` transitions an item to `in_progress` on
claim *except* for `merge` and `exec`. So a `planning` item stays `planning`
while its documents are written, which is correct: writing a plan is not
starting the work.

## 2. One binary, because the palette forbids a shell

`local-sd-db/sd_db/runner_exec.py:91` refuses any entry whose `argv[0]` basename is a shell
(`sh`, `bash`, `zsh`, `dash`, `fish`, `csh`, `tcsh`, `env`) or whose argv
contains `-c`, `--eval` or `-e`. There is no way to express an inline command.
`argv[0]` must further be an absolute path, executable, owned by root or the
invoking uid, and not group- or world-writable — and it is hashed into
`entry_sha256`, so replacing the file invalidates queued executions rather
than silently running new code.

So this needs a real file. It goes in a new folder, `local-sd-plan/`, with one
entrypoint `sd-plan.sh` — convention 1, the folder minus the `local-` prefix.

    local-sd-plan/sd-plan.sh item <id>   # plan one item, in the cwd's repo
    local-sd-plan/sd-plan.sh nightly     # select and enqueue, one per repo
    local-sd-plan/sd-plan.sh status      # 0 configured, 3 not, 1 broken
    local-sd-plan/sd-plan.sh help

`item` is what the palette entry runs; it is the executor. `nightly` is what
cron runs; it is only a selector, and every item it selects it enqueues
through the same `prepare()` the button calls. `status` is convention 6.

**Naming.** `sd-plan.sh` shares its name with the pack skill it invokes, and
that is deliberate: the script's whole job is to run `/sd-plan` unattended in
a clone. A different name would hide the relationship.

## 3. `item` is the executor, and it is thin

Standing in a runner-provided clone with an item id, `item <id>`:

1. reads the row (`sd_db.workflow.item_state`) and refuses an item whose
   `repo` is not the enclosing checkout — R10-D6, checked rather than trusted;
2. refuses an item that already has a `docs/work` sibling row, so a second
   trigger is a no-op rather than a second folder (acceptance criterion 3);
3. derives the slug from the row's date and title, the way the three existing
   folders are named;
4. runs `claude -p "/sd-plan <slug> --from sd:<id> ..." --dangerously-skip-permissions`, the prompt continuing (since 2026-09-17) with one sentence that says the run is unattended and names `prd.md`, `design.md` and `implement.md`,
   which is the one production pattern for headless invocation in this repo
   (`"$claude_bin" -p "$JOB_PROMPT"`, in `cmd_exec` of `local-cron-jobs/cron-jobs.sh`);
5. commits the three documents on the row's branch and **pushes them itself**;
6. runs the registration verb on what was written, and **fails if the folder
   exists and the row does not** — requirement 4, checked at the point it can
   still be reported.

Step 6 is the half that cannot be skipped. Step 4's `--from sd:<id>` does not
exist yet; §5 says where it comes from.

### Two things the runner does not do, discovered while building this

The first draft of this section had the runner commit and push, the way the
author role does. The code says otherwise on two counts, and both of them move
work out of the runner and into `item <id>`:

**Enqueue refuses any item without a valid branch, for every role including
`exec`.** `_item` raises `item N needs a valid branch`
(`local-sd-db/sd_db/runner.py:53`) before a role is even considered, and a row
made by `sd task add --here` has `branch` NULL. So neither the button nor the
nightly job can enqueue a backlog row as it stands: **the branch has to be on
the row before the enqueue**, which makes it PR 3's and PR 4's problem and not
something `item <id>` can paper over from inside the clone.

**The exec path records nothing, but it does publish the branch.** ~~The exec
path never pushes.~~ That is what this section said until the first end-to-end
run disproved it, and the correction matters because the rest of the design
leaned on it.

What is true: completion skips the item transition and the note for `exec`
(`local-sd-db/sd_db/runner.py:333`), so nothing about the run reaches the row.
What is false: that the branch stays in the clone. Closeout calls
`gitops.durable(run)` for every assignment whose role is not `reviewer`
(`local-sd-runner/sd_runner/runtime.py:572`), and `exec` is not `reviewer`, so
the leased branch is force-pushed with a lease on the way out
(`local-sd-runner/sd_runner/gitops.py:187`) — gated only on the clone being
clean, an uncommitted tree being archived instead
(`local-sd-runner/sd_runner/runtime.py:574-575`).

The run that showed this failed at `register()`, which raises before
`item <id>` reaches its own `git push`. The commit was on origin anyway. The
clone is still fitted with a `pre-push` hook pinned to the leased branch that
answers anything else with `runner: push outside leased branch refused`
(`local-sd-runner/sd_runner/gitops.py:160`), which is why the push that does
happen can only ever be that branch.

**The conclusion survives, for a different reason.** `item <id>` should still
commit and push itself — not because nothing else would, but because a run by
hand has no closeout to do it, and a command that behaves differently
depending on who invoked it is the thing this design set out to avoid. Under
the runner the two are redundant and harmless: `durable()` fetches, sees the
remote already contains the head, and returns without pushing
(`local-sd-runner/sd_runner/gitops.py:183-184`).

**What sets the branch is `sd runner prepare <id> --branch <name>`**, which
turned out to exist already: `bin/sd_runner.py:77` in the pack calls
`configure_item(connection, item, repo=str(root), branch=args.branch, ...)`.
It is a pure database write -- it takes `repo` from the enclosing checkout,
which is R10-D6 again, and touches git not at all. So nothing new has to be
built for this; `nightly` calls it before it enqueues, and a person pressing
the button runs it once by hand.

Together they set the contract: the branch exists on the row **before** the
enqueue, and `item <id>` commits and pushes on that branch itself. The
consolation is that this is the simpler design and not merely the forced one —
the same command now does the same thing run by hand in a working tree and run
by the runner in a clone, because it depends on the clone for nothing but a
checkout. That is what makes the by-hand verification in `implement.md` worth
anything.

### Five defects, and all five were the same defect

The first end-to-end run through the button found five faults in a row, and
nothing else found any of them: not the unit suite, not `sd-docs-lint`, not
eight green CI runs. Each was hidden one layer behind the last, so each fix
bought exactly one more layer of progress before the next one fired.

| # | What failed | Fix |
|---|---|---|
| 1 | `ImportError: cannot import name 'UTC'` — `python3` was Xcode's 3.9 | pin the interpreter, `local-sd-plan/sd-plan.sh:15` |
| 2 | the R10-D6 check refused the runner's clone by path | identity by remote, `local-sd-plan/sd_plan.py:92` |
| 3 | `FileNotFoundError: 'claude'` — not on the runner's `PATH` | resolve the binary, `local-sd-plan/sd_plan.py:178` |
| 4 | `Not logged in`, then **exit 0** — no `USER` for the keychain read | derive it from the uid, `local-sd-plan/sd_plan.py:207` |
| 5 | `register()` died on defect 1 again, in the script it shells out to | pin that interpreter too, `local-sd-db/sd-db.sh:8` |

Read as a list they look unrelated. They are one defect wearing four hats:
**every one is an assumption that the caller's shell had already arranged
something**, and the runner's environment arranges almost nothing. It is
`HOME`, `PATH` as `/usr/bin:/bin:/usr/sbin:/sbin`, `LANG`, and two git knobs
(`local-sd-db/sd_db/runner_exec.py:298`) — no profile, no login shell, no
`~/.local/bin`, no `USER`. A command written at a prompt inherits a PATH with
Homebrew on it, a `claude` on that PATH, and a `USER` naming the keychain to
read. The same command exec'd from the queue inherits none of those and says
so in four different vocabularies.

The rule this leaves behind, and it generalises past this item: **anything the
runner will execute must name its dependencies rather than resolve them.**
Name the interpreter, name the binary, name the identity. A bare name in a
palette command is a bet on an environment the palette does not provide.

The fifth is the proof that the rule has to be applied to the whole reachable
graph and not to the entrypoint you are looking at. Defect 1 was fixed in
`sd-plan.sh`; the run then got all the way to `register()`, which shells out
to `local-sd-db/sd-db.sh` — a *different* entrypoint, with its own
`PYTHON="${PYTHON:-python3}"`, which resolved to Xcode's 3.9 and failed with
the identical `ImportError` five layers later. Fixing the script you are
editing is not fixing the path the command takes.

The fourth is the one worth remembering, because it is the only one that did
not announce itself. The first three raised — a traceback is ugly but it is
unambiguous. The agent printed `Not logged in · Please run /login` and exited
**0**, so the failure arrived as a planning run that had simply written no
documents. The guard that caught it was already there for other reasons:
`item <id>` verifies all three documents exist before it commits anything. A
version of this that trusted the exit code would have committed nothing,
reported success, and left a row that looked planned.

## 4. `nightly` selects, and selection is a query

`sd_db.repos.registered()` reads the `repo` **table**
(`local-sd-db/sd_db/repos.py:238`). That matters for R10-D6: the job never
walks a filesystem looking for checkouts, it asks the database which
repositories exist. For each participating repository,
`sd_db.reads.backlog_items(connection, kind=..., repo=..., status='planning')`
returns unparked open items already ordered by priority then due date then id
(`local-sd-db/sd_db/reads.py:600`), and the job takes the first that has no
folder.

**One per repository per night, not two.** The original ask said two. One is
the recommendation: with 12 registered repositories, two is 24 unattended
planning sessions a night against an assumption (PRD §Assumptions) whose one
run has never been judged. One per repository is 12, still more than this has
ever produced in a day, and the number is a constant in one place.

## 5. Participation is a conf file, not a column

No opt-in mechanism exists — not on the `repo` table
(`local-sd-db/sd_db/schema/001_initial.sql:11`, and no later migration adds
one), not in a file. Two options:

- a `006_*.sql` migration adding a `repo.plans_nightly` column;
- a conf file in the new folder listing participating repository paths.

**The conf file.** The column is the wrong home for it: the `repo` table is
shared state that the pack, the dashboard, the runner and this repository all
read, and a column exists forever once added. Participation in one machine's
cron job is local policy, and `local-repo-sync/repos.*.conf` is the shape this
repository already uses for exactly that — a list of repositories, per
profile, that a scheduled job acts on. It also fails safe: an absent conf file
means nothing participates, which is the correct behaviour for a job that
writes branches.

## 6. The pack owes two things

Neither belongs here, and both are small:

- **`sd work register <path>`** in `bin/sd_work.py`. The logic is already
  shared — `register_work_item()` lives in `sd_db/workflow.py`, which the pack
  installs as a copy — so this is a parser and a print. `sd-plan` then
  registers what it writes, for every repository, not just this one.
- **`--from sd:<id>`** in `skills/sd-plan/SKILL.md`. The skill already seeds
  from `--from gh:...` and `--from jira:...` into the PRD's `## References`;
  the row adds `title`, `body` and `repo`, which is what the interview asks
  for. `sd task item <id>` already returns all of it. The `sd:` prefix is new
  syntax — every existing CLI takes a bare integer — and it is chosen to match
  the `Work: sd:<id>` trailer a person already writes.

`item` calls this repository's own `local-sd-db/sd-db.sh work register`, which
works here and nowhere else. The verb has since landed — `sd work register` is
in the pack as of `platypeeps/sd-ai-command-pack#816` — and the call did not
move, because the ordering constraint was never the verb. It is a runtime that
can execute it: `sd` resolves through the pack checkout, every cached runtime
under `~/.local/share/sd/runtimes/` carries an `sd_db` older than
`register_work_item` and `registered_for`, and the pack's own guard refuses
before reaching the row. Still not a permanent shape; the condition that ends
it is a provisioned runtime carrying the library, not the next pack release.

## 7. What is deliberately not built

- **A dedicated per-item button.** The dashboard has one generic Commands
  dialog with item and command pickers (`local-project-dashboard/sd_dashboard/pages.py:136`), and a
  registered entry appears in it immediately. A purpose-built button is
  dashboard UI work that changes nothing about whether the feature works.
- **Progress in the dashboard.** A queued mutating command shows
  "Queued assignment #N" and stops; the log is at `/api/executions/<note>`.
  Live progress is a separate change to `dashboard.js`.
- **A runtime budget of this item's own.** An `exec` assignment runs under
  the runner's default of 90 minutes (`budget_minutes=90`, in `enqueue` of
  `local-sd-db/sd_db/runner.py`; `source:local-sd-runner/sd_runner/runtime.py::_execute`
  sets the deadline from it and `source:local-sd-runner/sd_runner/runtime.py::_response`
  ends the child with "assignment time budget exceeded"), not the 24 hours
  this bullet cited until 2026-09-17. One planning run has been measured:
  exec note 709 on sd:442, 2026-09-11, started 15:58:54Z and ended
  16:14:43Z, 15 min 49 s, in which the agent wrote all three documents and
  the run failed at registration. The default is more than five times that
  run, so no budget of this item's own is set.

## Risks

- **Quality is unmeasured.** The PRD's last acceptance criterion is a human
  reading the output, and it is deliberately not automatable.
- **`--dangerously-skip-permissions` in a clone.** The clone is independent
  and disposable, which is the mitigation; the ceiling is the runner's.
- **The job is silent if the runner is not dispatching.** `nightly` enqueues
  and exits; if no runner claims the assignment, nothing happens and nothing
  says so. `status` exists to answer that, and `nightly` checks it first.
