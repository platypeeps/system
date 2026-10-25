---
title: A Build control queues ready items; a launchd dispatcher on the hub builds them headless
created: 2026-10-24
item: sd:6012
---

# Design — build-dispatcher

This page covers system sd:6012 (the dispatcher) and system sd:6004 (Build control and timed auto-pickup).
The rulings in their decision notes are settled: Q25, Q30-31, Q35, Q41, Q60, probe #28947, Paperclip read #28948.
Research: `docs/dashboard/orchestration-research-2026-10-24.md`, `paperclip-read-2026-10-24.md`, `ux-read-2026-10-24.md`.

## Problem

Today a lead session starts every builder by hand, watches it, reviews it and enqueues it.
Work stops when the lead's context decays or the operator sleeps.
Nothing records a builder run: no pid, no heartbeat, no cost, no outcome.
A builder that hangs holds its slot until someone notices.

## Approach

A Build press writes queue rows in the workflow database.
A launchd job on the hub ticks every two minutes, claims a bundle under the caps, and starts one supervisor per bundle.
The supervisor runs one bounded `claude -p` builder, then review and enqueue as steps.
No model sits in the dispatch loop.

### Reuse first

The pack still has the retired runner's tables (system sd:3041 and pack sd:3050 deleted its process side, not its schema).
They fit, so this design adds columns, not tables.

| Need | Reused | Added |
| --- | --- | --- |
| Queue row per pressed item | `assignment`: role `author`, lane `parallel`, scope `build` | nothing |
| Bundle | members' `assignment.parent` names the lead assignment | nothing |
| Run row per bundle attempt | `runner_run`: `run` is the attempt, `owner`, `work_path`, `supervisor_pid`, `supervisor_start`, `outcome`, `detail`, `cancel_requested`, `released_at` | `step`, `heartbeat_at`, `model`, `session_id`, `log_path`, `builder_pgid`, `builder_start`, `review_round`, `final` (migration 042) |
| Concurrency and branch lock | `runner_lease`, unique on active `(repo, branch)` | nothing |
| Cost and tokens | `cost` rows, `source='run'`, keyed to the lead assignment; `reads.item_assignments` already sums them | nothing |
| Item lock while queued or running | `workflow.change_status` refuses every status write while an assignment is queued or running (criterion 7.19) | nothing |
| Queue list and cancel | `sd assignments list|get|cancel`, the Operations and Tasks details cancel controls | a running build becomes cancellable |
| Toggle state | a `setting` row, `build.auto_pickup_until`; rows keep history | the key |
| Dispatcher liveness | a `heartbeat` state row, key `build-dispatch` | the key |
| Scheduling, timeout, skip-if-active | `local-cron-jobs`, as `lane-run.job` does | one `.job` file |
| Selection and caps | `start_next.plan`: ready rows of `reads.backlog_items`, builder count, load cap from `sd gate status --json` | moved into `sd_db.build`, shared |

Rejected: a new `runs`, `run_items` and `events` schema, and an `items.run_id` column, as the Paperclip read proposed.
The old tables already give the claim, the lease index, the cost ledger and the status guard; a second schema would duplicate them.
Rejected: a long-lived `KeepAlive` service loop.
A service restart on a pack refresh would end its children; a tick picks up new code at the next slot.

### Queue: the Build press

- A press on one or more ready items calls `build.queue(items, who, expected_revisions)` in one transaction.
- Each item must be `ready`, a runnable kind, in a managed repo with a remote, hosted on the hub (`repo.lane_host` NULL), and hold no active assignment.
- One refusal refuses the whole selection; nothing is written.
- Each item gets one assignment: role `author`, lane `parallel`, scope `build`, `budget_minutes` 60.
- The press then kickstarts the job, so a start takes seconds, not a slot.
- The item now refuses status writes until its assignment ends; Cancel is the way out.

### Claim: one `BEGIN IMMEDIATE`

The tick reads two values before the transaction, since neither is in the database:
the five-minute load and its cap (`sd gate status --json`), and the interactive builders (`claude_sessions.builders`).
An unread value means no claim this tick, never room.

Inside one `BEGIN IMMEDIATE` the tick:

1. Refuses while a restore reimport is pending (`runner.restoration_pending`).
2. Computes room: machine 6, repo 3, less active build leases, less interactive builders.
   An interactive builder counts against its repo when its cwd resolves to one; else against the machine only.
   A headless run's own session is excluded by its `session_id`, so it never counts twice.
   No room at all while load5 is above the cap.
3. Picks the repo with room whose oldest queued build assignment ranks first (priority, then queue time).
4. Takes up to `BUNDLE_MAX` (3) queued build assignments of that repo, re-checking each item in the WHERE:
   still `ready`, still runnable, no other active assignment.
5. Inserts one `runner_run` (step `claimed`, attempt `run_count + 1`) and one `runner_lease` (branch `build/sd-<lead>`).
6. Sets the members `running`, their `parent` to the lead, and moves each item `ready` to `in_progress` through `writes.transition`.
7. Rolls back if any row count differs from the bundle size.

### Dispatcher

- Job: `local-cron-jobs` `build-dispatch.job`, hub only, `JOB_SCHEDULE="*/2 * * * *"`, `JOB_TIMEOUT="90s"`.
  Command: `sd assignments dispatch`. A slot that finds the previous tick active logs `skipped`, as every job does.
- Each tick, in order: write the `build-dispatch` heartbeat; reap; watch the lane for enqueued runs; claim; spawn.
- Spawn starts `sd assignments supervise <run>` with `start_new_session=True`.
  It leaves the job's process group, so neither the job timeout nor launchd's exit cleanup reaches it.
- The supervisor records its pid and process start, then walks the run's steps from its `step` column.
  Every write names the run id and needs `released_at IS NULL AND outcome IS NULL`, so a late old supervisor writes nothing.
  A write that fails or changes no row stops the supervisor before its next external step (builder, commit, push, enqueue): it kills `builder_pgid` and exits.
- It beats `heartbeat_at` every 30 s for its whole life.
- Every child a step starts (`claude -p`, `sd-review`, `sd-ship`, `git`) runs in its own process group;
  the supervisor writes that group to `builder_pgid` and its start to `builder_start` before the child runs.
  Cancel, timeout and reap act on that one column, so they reach whatever step is active.

Steps, each written before it starts:

1. `worktree`: `git worktree add -b build/sd-<lead> ~/worktrees/<repo>-build-<lead> origin/main`.
   A retry finds the worktree on its branch and keeps it.
2. `building`: one `claude -p` in the worktree, in its own process group (`builder_pgid`):
   `--output-format stream-json --verbose --permission-mode auto --max-turns 300 --model <model>`.
   `CLAUDE_CODE_OAUTH_TOKEN` is set from `CUSTOM_CLAUDE_CODE_OAUTH_TOKEN` in the child's environment only; no row or log holds it.
   The prompt is a fixed pack template: the items, the worktree, the body and title paths, and the builder rules.
   It tells the builder the dispatcher owns the item status, and that its final message is its report.
   Output goes to `log_path`, `<bulk root>/<repo>/build/<run>.jsonl`.
3. Judge, by the final stream line, never the exit code (probe #28947).
   Pass: `type` `result`, `subtype` `success`, `is_error` false, `permission_denials` empty,
   and the worktree clean with commits ahead of `origin/main`.
   The line is stored in `final`; its cost and usage go to a `cost` row; its `result` text becomes a note on the lead item.
4. `reviewing`: `sd-review -C <worktree> --scope branch --json`.
   Blockers start `fixing`: `claude -p --resume <session_id>` with the blockers, under the same bounds, then review again.
   At most `REVIEW_ROUNDS` (3) rounds.
5. `enqueuing`: only after `kill(-builder_pgid, 0)` answers ESRCH, so no builder writes the branch.
   It checks the title (63) and the body (`sd-docs-lint --pr-body`), then `sd-ship -C <worktree> lane enqueue`.
6. `enqueued`: in one transaction the lease is released (the builder slot frees), the members end `done`, and the items stay `in_progress`.
   The run stays open: `released_at` and `outcome` stay null until step 7 ends it. The supervisor exits.
   Reap skips a run at `enqueued` only: the lane holds it, and no supervisor is expected.
   The lane moves the items to `done` when it merges.
7. The tick watches the lane entry. On `merged`, an auto-pickup run gets `checking` (below); every run then gets `cleaning`:
   the tick writes the step and a fresh `heartbeat_at` with a CAS on `step='enqueued'`, then spawns a supervisor.
   From then on the run is the supervisor's, and normal reap applies. Cleaning runs the entry's `remove` command once the worktree is clean.
   A lane `failed` or `skipped` entry ends the run `lane_failed`; the lane's own note names the step and reason.
   Cleaning, or `lane_failed`, writes `released_at` and the outcome last, in one transaction.

Model by work kind (Q60): `fields.work_kind` on the item, `mechanical` or anything else.
A bundle runs Sonnet only when every member is `mechanical`; else Opus. The dialog may override per bundle.
The run row logs the model.

### Liveness, timeout, recovery

- Alive: the supervisor pid answers `kill(pid, 0)` (EPERM counts as alive), `ps -o lstart=` matches `supervisor_start`,
  and `heartbeat_at` is under 180 s old.
- Timeout: the supervisor ends a `claude -p` past 60 min: TERM to `builder_pgid`, 20 s, KILL.
  `sd-review` keeps its own `--timeout`.
- Cancel: `sd assignments cancel` on a running build sets `cancel_requested`.
  The supervisor kills the active step's group (`builder_pgid`) at its next beat and waits for ESRCH, then keeps the worktree,
  ends the members `cancelled`, and moves the items to `ready` with a note. A group that outlives KILL blocks the run instead.
  From `enqueuing` on, cancel is refused with the lane entry to cancel: the lane may already hold the branch.
- Reap, each tick, for each unreleased run that fails liveness, except a run at `enqueued`:
  1. CAS `outcome='interrupted'` where `heartbeat_at` is the value read; a lost CAS means the supervisor beat, so leave it.
  2. Kill the supervisor's process group if its pid and start still match, then `builder_pgid` if any member lives.
     Confirm both gone before step 3. A group that outlives KILL sets `outcome='blocked'` and keeps the row and lease
     unreleased; each tick kills again and releases once ESRCH answers, so two writers never share the worktree.
  3. Recover, in one transaction (below).
- Recovery is one transaction, whoever finds the loss: reap, or the supervisor itself on a timeout or `error_max_turns`.
  It releases the old row (and its lease) with its outcome, then opens a fresh run row: `run` + 1, the same worktree and branch,
  the resume step from the ownership table, the old `review_round` and `session_id`, a fresh `heartbeat_at`, no supervisor yet.
  A step that holds a lease gets a new lease row for the fresh run. The tick spawns its supervisor.
  The fresh row's `outcome` is null, so its supervisor passes the write guard; the old row's is set, so a late old writer writes nothing.
- A second loss at the same step (a closed row of the assignment with that step and outcome `interrupted`, `timed_out` or
  `error_max_turns`) opens no fresh row: the ownership table's second-loss action applies.
- A run with `cancel_requested` set is closed `cancelled` instead; it opens no fresh row.
- A permission denial is no retry: it files a `--kind question` note naming the denied tool, and blocks the item.

### Run ownership by step

Every unreleased run row is owned by one process at a time. Reap acts only on a row a supervisor owns.

| Step | Owner | Group in `builder_pgid` | Lease | Reap when liveness fails | Recovery | Cancel | Test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `claimed` | the spawned supervisor, before its first write | none yet | held | kill and confirm any recorded pid | fresh row at `worktree`; second loss: blocked | closed `cancelled`, items `ready` | `test_reap_unstarted_run` |
| `worktree` | supervisor | `git` | held | kill and confirm | fresh row at `worktree`; second loss: blocked | kill, confirm, `cancelled`, items `ready` | `test_resume_worktree_fresh_row` |
| `building` | supervisor | `claude -p` | held | kill and confirm | fresh row at `building`, same worktree; timeout and max-turns too; second loss: blocked | as `worktree` | `test_resume_building_fresh_row` |
| `reviewing` | supervisor | `sd-review` | held | kill and confirm | fresh row at `reviewing`, round kept; second loss: blocked | as `worktree` | `test_resume_reviewing_keeps_round` |
| `fixing` | supervisor | `claude -p --resume` | held | kill and confirm | fresh row at `reviewing`, round kept: the review reads the head the fix left; second loss: blocked | as `worktree` | `test_resume_fixing_reviews_again` |
| `enqueuing` | supervisor | `sd-docs-lint`, `sd-ship` | held | kill and confirm | fresh row at `enqueuing`; it reads `lane list` for the item at its head first and records `enqueued` when the entry exists; second loss: blocked | refused, naming the lane entry | `test_enqueue_resume_no_duplicate` |
| `enqueued` | lane | none; the lane's runner keeps its own | released | skip | none needed: the tick moves it to `checking`, `cleaning` or `lane_failed` | refused, naming the lane entry | `test_reap_skips_only_enqueued` |
| `checking` | supervisor, fresh after the merge | `repo-sync.sh`, `dashboard.sh` | none | kill and confirm | fresh row at `checking`; second loss: health unknown, so auto-pickup off, push, item filed, then `cleaning` | refused: merged | `test_reap_kills_checking_group` |
| `cleaning` | supervisor | `git worktree remove` | none | kill and confirm | fresh row at `cleaning`; second loss: outcome `done`, worktree kept with a note | refused: merged | `test_reap_kills_cleaning_group` |
| `interrupted`, unreleased | reap, closing it | the dead row's groups | held | kill and confirm, then recover | the recovery transaction | `cancel_requested`: closed `cancelled`, no fresh row | `test_reap_resumes_kill` |
| `blocked`, unreleased (a group outlived KILL) | reap | the survivor | held | kill and confirm each tick | released once ESRCH answers; no fresh row | refused | `test_survivor_holds_lease` |
| `done`, `blocked`, `cancelled`, `lane_failed`, `interrupted`, `timed_out`, released | none | none | released | refuse: reap reads only unreleased rows | none; Retry on a blocked run is `runner.requeue`, a new claim | refused | `test_reap_ignores_released` |

### Auto-pickup

- State: `build.auto_pickup_until`, a UTC timestamp, or null for off.
  On writes the next 07:00 America/Denver (the operator may pick another end); Off writes null.
- Each tick while now is before the end, after the press queue: it selects ready items to fill the room.
  Selection is `build.eligible`: `ready`, managed repo, hub-hosted lane, runnable kind, not parked or scheduled,
  no active assignment, highest priority first. Never planning, blocked or repo-less items.
- It queues and claims them in the claim's transaction, `who` `auto-pickup`; the run row records the trigger.
- Questions a run hits are question notes for the morning, as Q25 rules.
- After each auto-pickup merge (dispatcher step 7), `checking`: `repo-sync.sh refresh <repo>`, then `dashboard.sh health`.
  A non-zero exit or an unknown answer turns auto-pickup off first, then sends the critical push (sd:5997), then files a system item.
  No auto-revert (Q30-31). No spend cap: the builder caps bound it.
- Two auto-pickup runs in a row that end `blocked` also turn it off, with no push; the row says why (operator, note #29486).
- Gate (Q35): auto-pickup ships only after a week of Build presses with no stuck or lost work.
  The trigger is its own sd item with a measurement (Slices, step 6).

## Non-goals

- No satellite dispatch: items whose lane is hosted elsewhere are refused with that reason.
- No spend cap, no budget tables, no approval stages.
- No auto-revert after a failed health check.
- No retirement of the interactive lead; Start next stays, and queues a Build after step 5.
- No Send to builder from the Review page; system sd:6020 can requeue through this queue later.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Press | assignments queued | stale revision, item not ready, active assignment | whole selection refused, nothing written | `test_queue_refuses_whole_selection` |
| Claim | run, lease, members running, items in_progress | tick killed in the transaction; a second claim races | SQLite rolls back; `BEGIN IMMEDIATE` serializes, the second sees the first's lease | `test_claim_race_respects_repo_cap` |
| Claim | same | item moved after the press | its re-check fails, row count differs, rollback | `test_claim_rolls_back_moved_item` |
| Spawn | supervisor pid and start | spawn fails, or dies before its first write | no beat for 180 s after the row's fresh `heartbeat_at`: reaped, a fresh row at the same step; second loss blocks | `test_reap_unstarted_run` |
| Worktree | branch and folder on disk | git fails; killed after the add | retry finds the worktree on its branch and keeps it; another branch there blocks | `test_worktree_step_resumes` |
| Building | builder process group, log | hang; supervisor dies with the builder alive | 60 min kill; reap kills the orphan group; a fresh row retries once | `test_timeout_kills_group`, `test_reap_kills_orphan` |
| Judge | `final`, cost row, report note | exit 0 with denials; no final line | denial: question note, blocked; no line is unknown, not a pass | `test_judge_last_line_not_exit` |
| Reviewing, fixing | review report, fix commits | review tool error; blockers after 3 rounds | unknown result retried once, then blocked; rounds spent: blocked with the blockers | `test_review_rounds_bound` |
| Enqueuing | lane entry | killed after the lane wrote the entry | the resumed step reads `lane list` for the item at its head first: no second entry | `test_enqueue_resume_no_duplicate` |
| Enqueued | lease released, members done | a late old supervisor finishes | its CAS names its own run and an open outcome: no row changes | `test_late_run_writes_nothing` |
| Reap | outcome interrupted, supervisor killed | heartbeat stale while the supervisor still runs | reap kills its group and confirms it gone before a retry; its next failed write stops it anyway; a survivor blocks the run | `test_reap_kills_live_stale_supervisor` |
| Reap | outcome interrupted, fresh row | tick killed between the CAS and the recovery transaction | the row is `interrupted` and unreleased, so the next tick kills, confirms and recovers it | `test_reap_resumes_kill` |
| Recovery | old row released, fresh row at the resume step | a late old supervisor writes after recovery | its row's outcome is set, so the write guard changes nothing and it stops | `test_late_run_writes_nothing` |
| Recovery, later step | fresh row at `reviewing`, `enqueuing`, `checking` or `cleaning` | the resumed step's supervisor dies too | a second loss at that step takes the ownership table's second-loss action | `test_second_loss_same_step` |
| Cancel | active step's group killed | cancel during reviewing or fixing | the step's child is in `builder_pgid` too: killed, ESRCH confirmed, then cancelled | `test_cancel_kills_the_review_child` |
| Cancel | none | cancel at enqueuing or later | refused, naming the lane entry; nothing changes | `test_cancel_after_enqueue_refused` |
| Enqueued | lease released, run open | reap sees no supervisor while the lane holds the run | reap skips `enqueued` only; `checking` and `cleaning` get a supervisor and normal reap | `test_reap_skips_only_enqueued` |
| Cancel | cancel_requested, members cancelled, items ready | supervisor dead at cancel | reap ends it as cancelled, not retried | `test_cancel_dead_supervisor` |
| Checking | auto-pickup setting, push, item | killed after health failed | result not recorded, so the next tick runs the check again; off is written before the push | `test_health_failure_turns_off_first` |
| Two blocked | auto-pickup setting off | killed after the second block | the next tick counts the last two auto-pickup outcomes again and writes off | `test_two_blocked_runs_turn_off` |
| Cleaning | worktree removed | worktree dirty or held | left in place with a note; the run ends `done` and releases | `test_clean_keeps_dirty_worktree` |

Guards this skips or moves: `runner.claim` and `runner.release` are not called; their exclusive-lease and delivery logic does not apply to builds.
`test_claim_respects_exclusive_lease` keeps the exclusive-lease rule for any other lease in the repo.

## Slices

One PR per step, each with its fail-first tests from the failure table.
Each title fits an sd item and a PR title (63 characters or less).

| Step | Repo | Title | Fail-first | Waits on |
| --- | --- | --- | --- | --- |
| 1 | pack | `feat(build): build queue and claim on the runner tables` | `test_claim_race_respects_repo_cap` | none |
| 2 | pack | `feat(build): dispatch and supervise headless builder runs` | `test_judge_last_line_not_exit` | 1 |
| 3 | system | `feat(cron-jobs): build-dispatch job on the hub` | `test_example_commands` fails on the missing command | 2 in the pin |
| 4 | pack | `feat(dashboard): Build control and Builds panel` | the Build route refuses a non-ready item with a 409 | 3 |
| 5 | pack | `feat(dashboard): Start next queues a Build for its plan` | Start next writes a queued build assignment, starts no herdr session | 4 |
| 6 | system | `chore(build): a week of Build runs, none stuck or lost` | none: a tracking item | 4 |
| 7 | pack | `feat(build): timed auto-pickup with health stop rules` | `test_health_failure_turns_off_first` | 6, system sd:5997 |

What each step contains:

1. Migration 042 and `sd_db.build`: queue, eligible, room, claim, CAS writes, reap; `sd assignments build`.
   Leaves a queue nothing runs.
2. `sd assignments dispatch` and `supervise`, the brief template, the judge, review and enqueue steps.
   Tests drive a fake `claude` that prints stream-json lines and can hang.
   A macOS suite proves a supervisor survives the job's exit under real launchd.
3. `local-cron-jobs/examples/build-dispatch.job` and the hub profile line.
4. Tasks Build control and bulk Build, the Queue Builds panel, Cancel and Retry. Q35 step 1 is complete here.
5. Start next keeps its plan and caps, and queues a Build for its items; herdr launch stays on Fix only (operator, note #29486).
6. The Q35 trigger, an sd item that must reach done. Its measurement is a query over `runner_run` for 7 days:
   at least 10 build runs, 0 `interrupted` without a finished retry, 0 items `in_progress` with no active run and no lane entry.
7. The auto-pickup setting, the toggle, the selection, the checking step and both stop rules.

## Dashboard

- Tasks: a Build control on each ready row with a repo, and Build in the bulk bar beside Close.
  The dialog shows each repo's bundle, its model, its branch and the room now; Build confirms.
  A queued row shows `queued` until its claim; a refused press says why.
- Queue: a Builds panel above the lanes.
  One row per run: items by title, repo, model, step, elapsed, last output age, cost so far, Cancel, Log.
  States: working, waiting on me (a question note or blocked), stalled (no output for 15 min, shown only), lost.
  A list of the last 24 hours' runs with outcome, and Retry on a blocked one (`runner.requeue`).
- Queue: the auto-pickup row: Off, or On until 07:00 MDT, with the end editable.
  Locked, with the trigger item named, until step 6's item is done.
  When a health failure or two blocked runs turned it off, the row says so; a health failure links the filed item.
- Health: the `build-dispatch` heartbeat goes stale after 6 minutes, shown with the job's log.
- The Now strip (system sd:6003) reads the same run rows.

## Acceptance criteria

- [ ] A Build press on two items in one repo makes one run, one branch and one lane entry; `sd assignments list` shows both `done`.
- [ ] A fake builder that hangs is killed at 60 min, retried once in the same worktree, then its item is `blocked` with an action note.
- [ ] A fake builder that exits 0 with a permission denial leaves its item `blocked` with a question note.
- [ ] With 6 builders running, a press stays `queued` until one ends.
- [ ] After a forced health failure, `build.auto_pickup_until` reads null before the push is sent.

## Risks

- launchd may reap a new-session child on some macOS build. Step 2's macOS suite proves it on the hub before step 3 lands.
- The interactive builder count reads transcripts; an unread one stops claims until it reads.
- A headless session may still show in `claude_sessions` as a builder; the `session_id` exclusion depends on that id being written before the next tick.
- The bulk root can be ejected. A claim refuses when the log folder is not writable; the panel shows that reason.
- A review pass is tied to the head it reviewed; enqueue checks the worktree head equals the reviewed head, else it reviews again.
- A judge pass is tied to the run's own log and session id; a log from another run is refused by its `session_id`.
- Cost is accepted without a cap, as ruled. Each run costs about $0.31-0.50 before work starts.

## Decided questions

The design review left six questions; all are decided.

1. Start next stays and queues a Build for its plan after step 4; herdr launch stays on Fix only. Operator, note #29486.
2. `BUNDLE_MAX` is 3, so a bundle fits the 60 min run. Lead, note #29482.
3. The dispatcher stops after 3 review rounds, then blocks the item with the blockers. Lead, note #29482.
4. Work kind comes from `fields.work_kind`; a missing field means Opus. Lead, note #29482.
5. Auto-pickup also turns off after 2 blocked runs in a row, with no push. Operator, note #29486.
6. The verbs fold into `sd assignments` (`build`, `dispatch`, `supervise`); no new group. Lead, note #29482.
