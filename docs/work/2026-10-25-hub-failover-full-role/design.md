---
title: The promoted work satellite holds the full hub role, and a returning hub writes nothing
created: 2026-10-25
item: sd:5986
branch: designs-5986
---

# Design — hub-failover-full-role

This page covers system sd:5986.
It builds on the failover that exists: `machine-setup.sh promote`, `--undo`, `--drill` and the fence
(system sd:3256, sd:4012, sd:4044, sd:4048; design `docs/work/2026-10-10-hub-failover/design.md`).
That page stays the reference for every promote and undo step this page does not change.

Binding rulings, by note:

- #28748 (Q3): the work satellite takes over by a manual promote and holds the full hub role and all accounts.
- #28774 (Q16): doctor runs `make check` on the pinned pack weekly on each lane-capable machine;
  a failover target that fails it is not ready.
- #28911 (Q55-Q56): promote restores the newest NAS backup of `sd.db`; no replica, no replication code;
  up to 1 day of loss is acceptable; the old hub must not write after it returns until it is demoted.
- #28914: hourly snapshots already reach the NAS (`sd-backups-hourly`).
- #28933 (Q67): the PR that implements this edits the rule lines it overrides; until then the old rules hold.
- #28934 (Q68): the employer's device policy allows personal accounts and repos on the work satellite.
- #31093, #31094, #31095 and #31089 answer this design's open questions; "Decided questions" lists them.

The item's first text asked for a warm replica. Q55-Q56 replaced it; this design builds no replica.

## Problem

Promote today moves the sd role only: the database, `sd-serve`, the dashboard, the backup jobs and the NULL-host lanes.
Q3 asks for more, and three gaps remain:

1. **Accounts and builders.** Mail, Drive, Slack and Notion work, and the headless builders, still need the hub.
   The old design made the hub's own agents a non-goal.
2. **A returning hub writes locally.** The fence stops the old hub's serve, lanes, NAS copies and `hub-pin` push.
   It allows local writes on purpose (old design, "The fence").
   So a session, job or dashboard on the returned hub still writes its stale `sd.db`. Q55-Q56 forbids that.
3. **Readiness proves no gate.** `hub-ready` checks pins, backups, plists and logins.
   Nothing proves the work satellite can pass the system and pack gates it would run (Q16).

The rule lines also still say this work happens only on the hub's own machine.

## Approach

Reuse the promote verb, its marker, the `hub-ready` block and the one role list.
Add three things: a write fence in the pack library, a weekly gate run with a `hub-ready` line, and the account agent in the role list.

### 1. Write fence on the returning hub (pack)

- Every write through the `sd_db` library on a hub asks `hub.role` before it commits, as `sd-serve` already does for remote writes.
  It reuses `serve.FENCE_SECONDS`: one Tailscale probe per writing process per 5 s.
- Write unless fenced (#31093): `holder`, `off` and `unknown` write; `elsewhere` refuses with `Fenced`.
  An unknown read counts as not fenced, so a Tailscale outage does not stop a healthy hub's writes.
  The refusal names the holder and the two demotion steps below.
- The existing fence callers keep their rule: serve, NULL-host lanes, NAS copies and the `hub-pin` push still refuse on `unknown`.
- This one guard covers every local writer: the CLI, the dashboard, job run reports and `task-actions`.
- Reads stay allowed, so the old hub's dashboard still shows its stale records.
- `sd-db restore` is the one exempt writer. Promote restores before this machine takes the hub's name.
- Rejected: a marker file the old hub reads at boot. The hub may return without a reboot, after a network cut.

Demotion stays two runbook steps, with no new verb (old design, "Demotion and return").
This design reverses their order, so the cut between them is safe:

1. Copy `~/.config/sd/hub.json` from any satellite. With `hub.json` beside `sd.db`, every `sd` command refuses (`HubConflict`).
2. Move `~/.local/share/sd/sd.db` to `sd.db.demoted-<date>`.

In the old order, a cut after the move left no `hub.json` and no database.
A nightly `machine-setup update` could then build a fresh database, whose unset `sd.hub_name` reads `off` and writes.

### 2. Weekly gate run on each lane-capable machine (system)

- `machine-setup.sh doctor gate`: a new doctor section, beside `doctor sd`.
  It runs `make check` in the system checkout, which uses its `.ci/pack` pin, then in the pack checkout.
  Each runs inside `sd gate run --max-wait 3600`, so it waits in the machine's gate queue.
  It prints the two heads it checked.
- `local-cron-jobs/examples/doctor-gate-weekly.job` runs it.
  The hub's and the work satellite's profiles list it: the machines that run `lane-run`.
- The job's `logs/.doctor-gate-weekly.stamp` holds only the last run, so a later try would hide an earlier verdict.
  So `doctor gate` keeps one record of its own, the newest decisive run, in
  `${XDG_STATE_HOME:-~/.local/state}/machine-setup/doctor-gate-last` (lead ruling, 2026-10-25):
  - a decisive run is a pass (both checks exit 0) or a real failure (a check exits non-zero, other than 75);
  - the record holds the verdict, the end time and the two heads, written to a temp file and renamed into place;
  - a timeout (exit 75, no gate slot) and a run killed before its verdict never write it.
- `hub-ready` gains one line from that record:
  - `ok` only when the record is a pass that ended within 8 days, naming its date and heads.
  - `NOT-READY` for a decisive failure, a pass older than 8 days, no record, or a record that cannot be read.
    Promote then refuses, as for every NOT-READY line.
  - A stamp newer than the record adds "last try timed out" (exit 75) or "last try did not finish" (any other exit).
    The note changes nothing else.
- So a pass Monday, a failure Tuesday and a timeout Wednesday read NOT-READY: Tuesday's failure is the record.
- The job's timeout exceeds the gate wait plus both checks, so the job does not kill a check that would finish.
- The pass names its machine (a local file) and its heads. It proves the toolchain, not today's pin. Risks says why.

### Readiness inputs: the class pass

Round 2 of the lane review found the same class twice:
a readiness line read from a record that a later run can overwrite or skip.
These are the inputs this design reads for readiness, and what each one reads from.

| Input | Read from | A later run that writes it |
| --- | --- | --- |
| Weekly gate | `doctor-gate-last`, the newest decisive run | `doctor gate`, on a verdict only |
| Role plists and job files (the new labels included) | the files on disk, read on each `status` | none; the operator places them |
| Role labels loaded after promote | `launchctl print`, asked on each promote run at step 9 | none; launchd's live state |
| Backup age | the newest snapshot's manifest checkpoint time, under the backup roots | the hourly backup and its NAS copy |
| Account logins | nothing: readiness does not test them (#31094) | none |
| The fence | `sd-db fence`, asked live by promote steps 7 to 9 | none; Tailscale's live state |

Only two inputs are records that a later run writes: the weekly gate and the backups.
The other inputs read live state on each run, so no earlier answer can stand in for a later one.

Backup age is a new `hub-ready` line. Today the block only checks that some dated snapshot exists.
If the hourly NAS copies stop, an old snapshot would still read `ok`, and a promotion would lose more than the 1 day Q55 allows:

- `ok` while the newest snapshot's checkpoint is under 24 hours old, naming its age.
- `NOT-READY` when it is older and the hub answers: the backups have stalled, so the standby is not ready.
- With the hub down, an old snapshot is still the best one there is. The line then reads `--` and names the age.
  The loss report names it too, so it never blocks a real failover.
- A manifest that cannot be read is no checkpoint: `restore --newest` already skips that candidate, and the line reads the next one.

### 3. The full role while promoted (system)

- `SD_HUB_ONLY_AGENTS` stays the one list of what moves with the role. It gains:
  - `google-workspace-mcp`, the agent that holds the mail and Drive logins;
  - `cron.night-build`, the hub's headless builder job;
  - the build dispatcher's job, once system sd:6012's job file exists.
- `hub-ready` already reads NOT-READY for a role label with no plist or job file.
  So the work satellite must hold these files before it is ready.
- Logins only after a promotion (#31094): workspace-mcp's two Google accounts, the Slack plugin and the Notion MCP
  stay off the work satellite until a failover.
  Promote's loss report gains an **Accounts** block: the manual login step for each, run on the work satellite.
  `hub-ready` does not test them in advance.
  `promote --undo` gains the reverse: each login to sign out of before the hand-back ends.
- The hub's other host jobs do not move (#31095): mail intake, briefs, home prep and HOA pause until the hub returns.
  The role moves sd, workspace-mcp and the builder jobs only.
- No secret is copied between machines. This keeps the old design's rule.

### 4. The rule lines (Q67)

The lines that bind work to the hub's own machine sit below the managed block of the hub's `~/.claude/CLAUDE.md`.
They are personal lines, not in this repository. `local-agent-prompt/prompt/shared.md` holds none of them:
a search on 2026-10-25 found none there, and none in the other files that `agent-prompt targets` lists.

| Section | Line, with the hub's name as `<hub>` |
| --- | --- |
| Machine roles | "Implement system and pack work only on `<hub>` (the hub), never on a satellite" |
| Machine roles | "A satellite session that finds system or pack work files an sd item or sends it to `<hub>`'s session" |
| Machine roles | "Use the work satellite only for steps that need its access (… not mail, Slack, Notion or Drive, which go through `<hub>`)" |
| Mail and Slack | the work mail, personal mail, Slack, Notion and Drive lines, each "… on `<hub>`" |
| Mail and Slack | "A session on another machine asks `<hub>`'s session … only `<hub>` holds these credentials" |

No repository PR can edit them (#31089):

- The implementing PR names the exact replacement lines in its body.
  Each names the role, not the machine: "the hub, the machine whose `sd-db fence` reads `holder`",
  with the failover exception for the promoted work satellite.
- The lead applies them by hand on each machine when the PR merges, and records that on system sd:5986.
  The work satellite's file is edited through its session, never over SSH.
- `shared.md` does not change.
- Session names stay as they are: on a promoted work satellite they remain `work-<repo>`.
  `SendMessage` finds them by name.

## Non-goals

- A live replica, WAL shipping or any replication code (Q55-Q56).
- Automatic failover. The operator decides that the hub is dead.
- A `demote` verb. The two runbook steps stay, in the new order.
- Copying any login or secret between machines.
- The hub's other host jobs: mail intake, briefs, home prep and HOA pause until the hub returns (#31095).
- Testing the account logins before a failover (#31094).

## Failure table

The promote and undo steps that this design leaves alone keep their rows in the old design's table.
These rows cover what changes.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Old hub returns | none: each write refuses | the fence reads `elsewhere` after the console rename | `Fenced`, naming the holder and the demotion steps; reads still work | `test_a_local_write_on_a_fenced_hub_refuses` (pack) |
| Old hub returns | stale local rows | Tailscale logged out or cut off, so the fence reads `unknown` | writes, by #31093; nothing leaves the machine, since serve, lanes, NAS copies and `hub-pin` refuse on `unknown`; the rows stay in `sd.db.demoted-<date>` | `test_an_unknown_fence_writes_locally` (pack) |
| Healthy hub | none | Tailscale outage, so the fence reads `unknown` | local writes go on (#31093) | `test_an_unknown_fence_writes_locally` (pack) |
| Old hub returns | none | a write already open when the fence turns | the check runs at commit, so that transaction rolls back | `test_a_write_open_across_the_rename_rolls_back` (pack) |
| Promote 5 restore | `sd.db` installed | the write fence refuses restore, since the name is not taken yet | `sd-db restore` is exempt; every other write still asks | `test_restore_writes_before_the_name_is_taken` (pack) |
| Promote 9 report | Accounts block printed | a login is not done when the operator reads it | nothing moved; the block repeats on every rerun until the marker goes | `test_the_report_lists_each_account_login` |
| Demote 1 | `hub.json` copied beside `sd.db` | cut before step 2 | every `sd` refuses (`HubConflict`); the stage builds no database; step 2 runs later | `test_a_demotion_cut_after_hub_json_writes_nothing` |
| Demote 2 | `sd.db` moved aside | the operator follows the old order from memory | the README and the `Fenced` text name only the new order, `hub.json` first | `test_the_refusal_names_hub_json_first` (pack) |
| Undo 1 | role stopped, including the account agent | the agent's bootout fails | as for every role label: no snapshot, the role starts again | `test_a_role_label_the_satellite_profile_lists_stops_too` covers the list |
| Undo end | logins listed | the operator skips a sign-out | the report names each one; the hand-back still ends. Risks says why | `test_undo_lists_each_account_to_sign_out` |
| Weekly gate | record: a failure | `make check` fails | NOT-READY until a later pass; promote refuses | `test_a_failed_weekly_gate_is_not_ready` |
| Weekly gate | stamp only; the record is unchanged | a later slot timeout (exit 75) | the record decides: a pass under 8 days stays `ok` with "last try timed out", and a failure stays NOT-READY | `test_a_timeout_after_a_failure_stays_not_ready` |
| Weekly gate | stamp only | a pass, then a failure, then a timeout | the failure is the record, so the line reads NOT-READY | `test_pass_fail_timeout_reads_not_ready` |
| Weekly gate | stamp only | a later run killed before its verdict | no record write; the old verdict stands, with "last try did not finish" | `test_a_killed_gate_run_keeps_the_last_verdict` |
| Weekly gate | record | killed while writing the record | temp file and rename: the old record or the new one, never part | `test_a_record_cut_short_keeps_the_old_one` |
| Weekly gate | none | no pass in 8 days, no record, or an unreadable record | NOT-READY, naming the record | `test_no_pass_in_8_days_is_not_ready` |
| Readiness: role files | none | a later edit removes or breaks a plist or job file | read on each `status`, so the next read is NOT-READY; step 9 asks launchd for each label after the install | `test_a_role_label_with_no_plist_is_not_ready`; the old design's step-9 launchd tests |
| Readiness: role files | none | `status` cut short mid-block | `status` is read-only, so nothing is left; the next run reads again | none new; read-only |
| Readiness: backup age | newest snapshot | later hourly backups or NAS copies fail or stop | the newest checkpoint ages; past 24 hours with the hub up, NOT-READY | `test_a_stalled_backup_is_not_ready_while_the_hub_answers` |
| Readiness: backup age | newest snapshot | a later copy is cut short and leaves a partial snapshot | its manifest does not verify, so the line and `restore --newest` read the next one | `test_a_partial_snapshot_is_not_the_newest` |
| Readiness: backup age | none | the roots time out on listing | the existing lines read `--` or NOT-READY for the roots; no age reads `ok` | `test_an_unlisted_root_gives_no_age` |
| Readiness: backup age | none | the hub is down and the snapshot is old | `--` with the age; the loss report names it; promote goes on | `test_an_old_snapshot_with_the_hub_down_does_not_block` |
| Readiness: logins | none | not read (#31094) | promote's Accounts block lists each login on every run until the marker goes | `test_the_report_lists_each_account_login` |
| Readiness: fence | none | Tailscale changes between steps | promote asks again on every run from step 7 (old design) | `test_a_rerun_at_step_9_asks_the_fence_again` |
| Older state | none | a pack without the write fence (rollback) | the returned hub writes locally, as today; NAS copies and `hub-pin` stay fenced | none new; named here |
| Older state | none | `sd.hub_name` unset | the fence reads `off`: every write runs, as before | `test_an_unset_hub_name_writes` (pack) |

## Slices

1. **pack: the write fence.** `feat(sd-db): fence local writes on a returning hub`.
   Fail-first: `test_a_local_write_on_a_fenced_hub_refuses`.
   Leaves working: the live hub reads `holder` and writes as today.
2. **system: the weekly gate and backup age.** `feat(machine-setup): hub-ready reads gate verdict, backup age`.
   Fail-first: `test_pass_fail_timeout_reads_not_ready` and `test_a_stalled_backup_is_not_ready_while_the_hub_answers`.
   Waits on nothing.
3. **system: the full role and the rule lines.** `feat(machine-setup): promote moves accounts and builders`.
   The role list, the Accounts and sign-out blocks, and the demotion order in the README.
   Its body names the replacement rule lines; the lead applies them at merge (#31089). Waits on slice 1 in the pin.
4. **Drill.** One `promote --drill` on the work satellite after slice 3, quoted in slice 3's body.

## Acceptance criteria

- [ ] On a test hub whose fence reads `elsewhere`, `sd task note 1 x` exits non-zero and names the demotion steps.
- [ ] On the live hub, the fence reads `holder` and a note writes.
- [ ] `machine-setup.sh status` on the work satellite shows a `hub-ready` gate line that reads `ok`, with a date under 8 days old.
- [ ] With the hub up and the newest snapshot over 24 hours old, the backup age line reads NOT-READY.
- [ ] `promote --drill` on the work satellite passes, and its report lists the Accounts block.
- [ ] After the lead applies slice 3's lines, a search for the hub's machine name in the "Machine roles" and "Mail and Slack" lines of each machine's personal block finds nothing.
- [ ] `sd-docs-lint` passes from the repository root.

## Risks

- **A returning hub with Tailscale off writes locally.** The fence reads `unknown`, which writes (#31093).
  Accepted: failover is manual and rare, nothing leaves the machine, and demotion moves those rows aside.
- **Probe cost.** One `tailscale status --json` per writing process per 5 s.
  A probe with Python start-up took 0.17 s on the hub on 2026-10-25. Slice 1 measures the probe alone and quotes it.
- **Failed jobs on a returning hub.** Its jobs fail their sd writes and may push phone alerts.
  Accepted: the first alert tells the operator to demote it.
- **The weekly pass proves the toolchain, not today's pin.** Pins move daily, and a check at each pin would mean a full gate per merge.
  Accepted, per Q16's weekly rule. The line names the heads it passed.
- **Logins left behind.** A sign-out the operator skips leaves a personal login on the work machine. Q68 allows it; the undo report names it.
- **One machine runs both lane sets while promoted.** The old design accepts this; builders add load. The dispatcher's load cap bounds it.

## Decided questions

The design left four questions; all are decided.

1. Write unless fenced: an unknown fence read counts as not fenced. Operator, #31093, answering #31081.
2. Logins go on the work satellite only after a promotion; promote lists the manual step; readiness does not test them.
   Operator, #31094, answering #31082.
3. The other host jobs do not move; they pause until the hub returns. Operator, #31095, answering #31083.
4. The implementing PR names the replacement rule lines in its body; the lead applies them by hand at merge.
   Lead, #31089, answering #31084.
