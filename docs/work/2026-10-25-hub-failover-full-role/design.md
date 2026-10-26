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
- `holder` and `off` write. `elsewhere` and `unknown` refuse with `Fenced`.
  The refusal names the holder and the two demotion steps below.
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
- `hub-ready` gains one line from the job's `logs/.doctor-gate-weekly.stamp`:
  - `ok` for `exit=0` ended within 8 days, naming the date and the heads.
  - `--` for exit 75, a gate slot that never came free.
  - `NOT-READY` for any other exit, no stamp, or one older than 8 days. Promote then refuses, as for every NOT-READY line.
- The pass names its machine (a local stamp) and its heads (the log). It proves the toolchain, not today's pin. Risks says why.

### 3. The full role while promoted (system)

- `SD_HUB_ONLY_AGENTS` stays the one list of what moves with the role. It gains:
  - `google-workspace-mcp`, the agent that holds the mail and Drive logins;
  - `cron.night-build`, the hub's headless builder job;
  - the build dispatcher's job, once system sd:6012's job file exists.
- `hub-ready` already reads NOT-READY for a role label with no plist or job file.
  So the work satellite must hold these files before it is ready.
- Interactive logins cannot move: workspace-mcp's two Google accounts, the Slack plugin and the Notion MCP.
  Promote's loss report gains an **Accounts** block, listing each login to run on the work satellite.
  `promote --undo` gains the reverse: each login to sign out of before the hand-back ends.
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

The edit names the role, not the machine:

- `shared.md` gains one line under "Other machines": the hub is the machine whose `sd-db fence` reads `holder`.
  After a promotion that is the work satellite, until `promote --undo` hands the role back.
- The rollout rewrites "on `<hub>`" as "on the hub" in each machine's personal lines.
  The work satellite's file is read through its session, never over SSH.
- Session names stay as they are: on a promoted work satellite they remain `work-<repo>`.
  `SendMessage` finds them by name.

## Non-goals

- A live replica, WAL shipping or any replication code (Q55-Q56).
- Automatic failover. The operator decides that the hub is dead.
- A `demote` verb. The two runbook steps stay, in the new order.
- Copying any login or secret between machines.
- The hub's other host jobs, such as mail intake, briefs and home prep. Open question 3 asks.

## Failure table

The promote and undo steps that this design leaves alone keep their rows in the old design's table.
These rows cover what changes.

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Old hub returns | none: each write refuses | the fence reads `elsewhere` after the console rename | `Fenced`, naming the holder and the demotion steps; reads still work | `test_a_local_write_on_a_fenced_hub_refuses` (pack) |
| Old hub returns | none | Tailscale logged out or cut off, so the fence reads `unknown` | refuses as `elsewhere` does; a failed probe is no answer | `test_an_unknown_fence_refuses_a_local_write` (pack) |
| Old hub returns | none | a write already open when the fence turns | the check runs at commit, so that transaction rolls back | `test_a_write_open_across_the_rename_rolls_back` (pack) |
| Promote 5 restore | `sd.db` installed | the write fence refuses restore, since the name is not taken yet | `sd-db restore` is exempt; every other write still asks | `test_restore_writes_before_the_name_is_taken` (pack) |
| Promote 9 report | Accounts block printed | a login is not done when the operator reads it | nothing moved; the block repeats on every rerun until the marker goes | `test_the_report_lists_each_account_login` |
| Demote 1 | `hub.json` copied beside `sd.db` | cut before step 2 | every `sd` refuses (`HubConflict`); the stage builds no database; step 2 runs later | `test_a_demotion_cut_after_hub_json_writes_nothing` |
| Demote 2 | `sd.db` moved aside | the operator follows the old order from memory | the README and the `Fenced` text name only the new order, `hub.json` first | `test_the_refusal_names_hub_json_first` (pack) |
| Undo 1 | role stopped, including the account agent | the agent's bootout fails | as for every role label: no snapshot, the role starts again | `test_a_role_label_the_satellite_profile_lists_stops_too` covers the list |
| Undo end | logins listed | the operator skips a sign-out | the report names each one; the hand-back still ends. Risks says why | `test_undo_lists_each_account_to_sign_out` |
| Weekly gate | stamp written | `make check` fails | NOT-READY; promote refuses until a pass | `test_a_failed_weekly_gate_is_not_ready` |
| Weekly gate | stamp written | no gate slot within the wait: exit 75 | reads `--`, not a pass and not a fail | `test_a_gate_that_found_no_slot_is_unread` |
| Weekly gate | none | no stamp, or one older than 8 days | NOT-READY, naming the job | `test_a_missing_or_stale_gate_stamp_is_not_ready` |
| Older state | none | a pack without the write fence (rollback) | the returned hub writes locally, as today; NAS copies and `hub-pin` stay fenced | none new; named here |
| Older state | none | `sd.hub_name` unset | the fence reads `off`: every write runs, as before | `test_an_unset_hub_name_writes` (pack) |

## Slices

1. **pack: the write fence.** `feat(sd-db): fence local writes on a returning hub`.
   Fail-first: `test_a_local_write_on_a_fenced_hub_refuses`.
   Leaves working: the live hub reads `holder` and writes as today.
2. **system: the weekly gate.** `feat(machine-setup): weekly doctor gate and its hub-ready line`.
   Fail-first: `test_a_failed_weekly_gate_is_not_ready`. Waits on nothing.
3. **system: the full role and the rule lines.** `feat(machine-setup): promote moves accounts and builders`.
   The role list, the Accounts and sign-out blocks, the demotion order in the README, and the `shared.md` line.
   Its rollout rewrites each machine's personal lines (Q67). Waits on slice 1 in the pin.
4. **Drill.** One `promote --drill` on the work satellite after slice 3, quoted in slice 3's body.

## Acceptance criteria

- [ ] On a test hub whose fence reads `elsewhere`, `sd task note 1 x` exits non-zero and names the demotion steps.
- [ ] On the live hub, the fence reads `holder` and a note writes.
- [ ] `machine-setup.sh status` on the work satellite shows a `hub-ready` gate line that reads `ok`, with a date under 8 days old.
- [ ] `promote --drill` on the work satellite passes, and its report lists the Accounts block.
- [ ] After slice 3's rollout, a search for the hub's machine name in the "Machine roles" and "Mail and Slack" lines of each machine's personal block finds nothing.
- [ ] `sd-docs-lint` passes from the repository root.

## Risks

- **A Tailscale outage on the healthy hub stops its local writes.** The fence cannot tell that from a return.
  Open question 1 asks. Remote writes already stop then, since satellites reach the hub over Tailscale.
- **Probe cost.** One `tailscale status --json` per writing process per 5 s.
  A probe with Python start-up took 0.17 s on the hub on 2026-10-25. Slice 1 measures the probe alone and quotes it.
- **Failed jobs on a returning hub.** Its jobs fail their sd writes and may push phone alerts.
  Accepted: the first alert tells the operator to demote it.
- **The weekly pass proves the toolchain, not today's pin.** Pins move daily, and a check at each pin would mean a full gate per merge.
  Accepted, per Q16's weekly rule. The line names the heads it passed.
- **Logins left behind.** A sign-out the operator skips leaves a personal login on the work machine. Q68 allows it; the undo report names it.
- **One machine runs both lane sets while promoted.** The old design accepts this; builders add load. The dispatcher's load cap bounds it.

## Open questions

Each is filed on system sd:5986 as a question note.

1. Note #31081: when the fence cannot be read (`unknown`), may the hub still write locally? Recommended: refuse.
2. Note #31082: do the account logins wait for a promotion, or stay on the work satellite all the time? Recommended: wait for a promotion.
3. Note #31083: do the hub's other host jobs (mail intake, briefs, home prep) move with the role? Recommended: no, they wait for the hub.
4. Note #31084: Q67 says the PR edits the rule lines, but those lines are untracked personal lines.
   Recommended: the PR adds the role line to `shared.md`, and its rollout rewrites the personal lines.
