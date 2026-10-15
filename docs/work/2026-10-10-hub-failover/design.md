---
title: A satellite becomes a temporary hub from the newest verified sd backup
created: 2026-10-10
item: sd:3256
---

# Design: hub failover (sd:3256)

## Problem

One machine, the hub, holds the workflow database (`~/.local/share/sd/sd.db`) and serves it to satellites.
If the hub dies, every satellite stops: `HubUnreachable` on each sd verb, no lane merges, no dashboard.
No scripted path turns a satellite into a hub. The parts exist (`sd-db.sh restore`, the machine-setup stages, `SD_HUB_ONLY_AGENTS`), but nobody has joined them.

The biggest gap is backup reach. Measured on 2026-10-10 at 12:22 MDT, read-only:

| Fact | Value |
| --- | --- |
| Live database | 754 MB, WAL 4 MB |
| One snapshot directory | 783 MB (`sd.db` plus journals, manifest and configuration) |
| Hourly snapshots (`sd-db-backup-hourly`, :50) | on the hub's USB disk only, `Backup Local/sd-backups`; 24 in the last 24 h; 216 kept, 117 GB |
| Nightly snapshot (`sd-db-backup`, 02:10) | on the USB disk, then chained to the NAS share (additive copy); 33 on the NAS, 15 GB |
| Newest snapshot off the hub | 02:10 MDT, so 10 h 12 min old at measurement |
| Hourly snapshots on the NAS | newest from 2026-09-27: `nas-full-copy` runs out of its 23:00 to 05:00 window before it reaches them |
| Hub configuration tarball (`system-config-backup`) | on the USB disk only |
| Copy the newest NAS snapshot to a local disk | 4.9 s |
| Full `restore` of that copy into a throwaway home, migration 24 to 26 included | 5.0 s |

So the recovery point off the hub is up to 24 hours today, and 48 after one failed night.
The hourly snapshots give 1 hour only when the USB disk survives and moves to the satellite.
The restore itself is fast: about 10 seconds from share to an installed database.
The recovery time is set by the role change and by people, not by the data.

## Approach

One verb on the chosen satellite, `machine-setup.sh promote`, joins existing parts.
It reads the newest verified snapshot it can reach, restores it, installs the hub role and takes the hub's name.
Each section below answers one exploration point of the item.

### The verb and its steps

`machine-setup.sh promote [--from DIR] [--apply]`. A dry run prints the plan and the candidate snapshot, as every machine-setup verb does.

1. **Preflight, no writes.** This machine is a satellite (`hub.json` exists) with no local `sd.db`.
   The hub does not answer on its serve port within 30 s, name lookup included.
   No readiness line (below) reads `NOT-READY`.
   The newest candidate's schema is at or below this machine's library: `restore --newest` stops on a newer one before it writes.
   `sd.hub_name` cannot be read here: it lives in the dead hub's database. Step 7 reads it from the restored one.
2. **Drain.** Hold this machine's lane locks and ship locks and wait for its gates from before, through `refresh_drain.py`. The lane root comes from this machine's config, never through `sd`, which may reach the dead hub.
   A lane this satellite hosts keeps its ship lock under `$XDG_STATE_HOME/sd/ship-locks/`; after step 4 the lock lives beside the database, so no ship may run across the move.
3. **Intent marker.** Write `${XDG_STATE_HOME:-~/.local/state}/sd/promote-intent` atomically, as `follow-intent` does (sd:3100).
   It names the step reached and, after step 5, the snapshot chosen.
4. **Leave the satellite role.** Move `~/.config/sd/hub.json` to `hub.json.promoted-<UTC stamp>`.
   `restore` refuses with `HubOnly` while the file exists, so this step comes first.
5. **Restore.** `sd-db restore --newest <roots>`: a new option on the existing verb.
   A restore that installs nothing puts `hub.json` back and deletes the marker.
   It lists every dated directory under the roots, orders them by the manifest's checkpoint time, not by name, and tries the newest first.
   It copies a candidate off the share (as `offsite-verify.py` does), then runs the whole `restore`.
   A refused candidate prints its reason, and the next one is tried.
   `restore` already migrates the staged copy, blocks queued and running assignments, and leaves the `restore` hold row open.
6. **Settle stale rows.** In one transaction, release each open `runner_lease` and note its run `abandoned at promotion`.
   A lease names a supervisor pid on the dead hub; a pid check on this machine could match an unrelated process.
   `restore --newest` does this itself (slice 3), so the marker's step 6 is the seed instead:
   copy the restored snapshot into the hourly job's local root (`SD_DB_BACKUP_HOURLY_DESTINATION`), so the next hourly run numbers after it.
   With an empty root the day's names restart, and the additive NAS copy writes into the NAS folders of the same names (sd:4012 note).
7. **Take the hub's name.** First, `sd-db fence` must not read `off`: the restored database must hold `sd.hub_name`, else promote refuses and names `sd config set` (decision on sd:4044).
   If no tailnet peer holds the hub's name, run `tailscale set --hostname=<hub name>`.
   If the dead node still holds it, stop and print the steps: stop that node (power off, or unload its `sd-serve` and lane agents; if unreachable, remove it in the Tailscale admin console), then rename or remove it there. The runbook asks for the stop before every promote; the fence's `Self.Online` check is the first guard (sd:4044).
   A rerun continues here. The step ends only when the fence reads "holder".
8. **Install the hub role.** Run the `sd`, `cron` and `agents` stages with the hub role on (see "Hub role" below).
   They add the hub-role labels only when the fence reads "holder", so this step must follow step 7; a rerun from the marker keeps that order.
   That starts `sd-serve`, the dashboard and its `tailscale serve` route, `task-actions` and its funnel, the backup jobs and `offsite-verify`.
   `lane-run` already runs on every machine; `hosts_lane` now answers yes for each lane whose host is NULL.
9. **Prove and report.** Open one tailnet session to this machine's `sd-serve` and run `sd task show 1`.
   Print the loss report (below). Delete the marker.

Dispatch stays paused until the operator reads the report and runs `sd restore resume`, the existing step after any restore.

### Backup reach and the recovery point

Recommended (decision D1): after each hourly snapshot, mirror the hourly root to the NAS share, in the same job.
This copies the nightly job's chain exactly: `sd-db.sh backup && offsite-verify.py --preflight-only && mirror-sync.sh sync`.
The copy is additive (`MIRROR_SYNC_ADDITIVE`), with its own conf and its own NAS folder, `Backup/sd-backups-hourly`.
An exact mirror was rejected: a promoted hub starts with an empty local root, and its first mirror would delete the NAS history it was restored from.
The NAS keeps its own window: after the copy, the same job deletes NAS directories whose manifest checkpoint is more than 7 days old, reusing the backup job's owned-backup retention code.
Only the fence holder runs that prune, as the `--preflight-only` gate already ensures.
The local hourly root keeps its newest 24 snapshots (`--keep 24`, ruling sd:3637), about 19 GB; the NAS keeps 7 days by checkpoint age, about 130 GB.
Each run copies one 783 MB directory and prunes at most one.
`offsite-verify` gains a second root with a 3-hour age limit, so a stalled hourly copy mails the next morning.

The recovery point becomes 1 hour, or the nightly 24 hours when the NAS was down.
WAL shipping or a short-interval snapshot is rejected: it needs a new tool (for example a WAL replicator) and a new failure mode, for less than an hour.
For one operator, an hour of lost rows is an inconvenience, and the loss report names it.

`--from DIR` adds a root, for the case where the hub's USB disk survives and is plugged into the satellite.

### What lives beside the database

`restore` puts back what the backup carries. The 5.0 s restore above laid out:
`sd.db`, `providers.yaml`, `commands.yaml`, `executions/` (with `execution-evidence.json`), `publications/`, `runner-journal/`,
`operation-locks/`, `publication-recovery-evidence/` and `runner-recovery-evidence/`.
All sit under `~/.local/share/sd/`, where the hub's code reads them.

The hub's state folder also holds what no backup carries:
`evidence/`, `jev-corpus/`, `jev-snapshots/`, `runner-ending/`, `runtime-backups/`, `runtimes/`, `ship-locks/` and the writing cutover files.
Lane queues and gate slots live under `~/.local/state/sd/`, also unbacked.
The loss report lists these as lost. None of them blocks a hub from running; each is evidence or a cache.
Lane queues are the one loss with work in it; see "Lanes and gates".

### Hub identity

Recommended (D3): the hub's tailnet machine name moves; `hub.json` stays the same on every satellite.
A satellite's `hub.json` names `hub.example.test` (the hub's MagicDNS name). After step 7, that name resolves to the promoted machine.
The dashboard origin (`https://<name>...:8443`) and the task-actions funnel URL in sent mails keep working, because both are built from the node name.
The cost is one manual console step per failover: the dead node gives up the name. No tool can do it without a Tailscale API key, a new secret.

Rejected: a Tailscale Service name. Service hosts must be tagged nodes, and `serve` refuses to start on a tagged node (R8 of the second-machine plan).
Tailscale's Services page says: "Any device you want to use as a Service host must use a tag-based identity" ([kb/1552](https://tailscale.com/kb/1552/tailscale-services), read 2026-10-13).
Rejected as default, kept as the fallback: rewrite `hub.json` on every satellite. It needs a session on each, and every URL changes.

### The fence: the old hub returns

A returning old hub has a local `sd.db` and no `hub.json`, so today it acts as the hub at once: launchd starts its jobs at boot.
Its lane run would merge from a stale queue, its backup chain would copy stale snapshots onto the NAS with names the new hub also uses,
and a later `refresh` would push `hub-pin`.

The fence (D5): a machine holds the hub role only while its own tailnet name equals the hub's name.
The hub's name is one fleet setting, `sd.hub_name`, held in the hub's database (`sd config set sd.hub_name hub.example.test`).
After the console step, the old node is named something else, or is logged out. Its check reads "not the holder" or "unknown".
With the setting unset the fence reads "off": every caller runs as before, `sd-db fence` reports it, and `promote` refuses (decision on sd:4044).

| Caller | Holder | Not holder | Unknown (Tailscale does not answer) |
| --- | --- | --- | --- |
| `sd-db.sh serve` at start | serves | exits 1, names the holder | exits 1 (it cannot bind a tailnet address anyway) |
| `hosts_lane` for a NULL host | yes | `LaneElsewhere` | `LaneUnknown`: nothing merges |
| `offsite-verify.py --preflight-only`, the gate before every NAS copy | passes | refuses | refuses |
| `repo-sync.sh refresh`, the `hub-pin` push | pushes | refuses | refuses |

Local writes stay allowed: a demoted machine still writes snapshots to its own disk, and the dashboard still reads.
Only the writes that leave the machine are fenced.
A NAS-held claim file was rejected: a NAS outage would then either stop a healthy hub or let a demoted one through.

### Lanes and gates

- Lane queues lived in the dead hub's lane root, so they are gone. Items keep their sd status.
  The loss report lists each repository whose lane host is NULL, with its open pull requests, for `sd-ship lane enqueue` again.
- `repo.lane_host` NULL means "the hub", so those lanes follow the role with no row change.
  A row that names another satellite stays on it. A row that names the promoted machine's `hostname -s` stays on it, which is now the hub.
- Gate slots are machine-local lock files. The backup carries none, so nothing stale moves.
- Repository ship locks lived beside the dead hub's database and are not in the backup. Step 2's drain covers this machine's own.
- A satellite's write that was in flight at the crash asks the new hub `outcome(R)`. The answer is `absent`, which is true for the restored history: the client raises `TransactionLost`, and the operator runs the verb again.

### Hub role on a satellite

Today a machine is the hub because its profile's `.agent` lists `<prefix>.sd-dashboard`.
Promotion must not swap this machine's whole profile: its apps, its jobs and its own role stay.
So the hub role becomes a state, not a profile: the `agents` and `cron` stages add the hub-role labels when this machine holds the role (no `hub.json`, a local `sd.db`, and the fence reads "holder").

The hub-role labels are `SD_HUB_ONLY_AGENTS` minus `cron.mirror-sync-nightly`.
That job mirrors the hub's own folders to iCloud, and a satellite has no such folders. One list stays the source; the exception sits beside it in one line.

Plists come from the config folder's `launchagents/`, else from the tracked examples, rendered with `@HOME@`, `@LABEL@` and `@ROOT@`.
`task-actions` installs through its own `task-actions.sh start`.
Job files come from the satellite's config `cron-jobs/jobs/`; the readiness check makes sure they are there.

### Secrets and machine-held values

| What | Where it lives today | On a promoted satellite |
| --- | --- | --- |
| GitHub token (MCP server) and `gh` login | per machine | readiness checks `gh auth status` and push access to each NULL-host repository |
| Mail for backup failures (`local-notify`) | `<config>/notify/.env`, per machine | readiness checks that the file names every variable `notify.sh` needs |
| `tailscale serve` (dashboard) and `funnel` (task-actions) | per node | step 8 sets both; the funnel needs the tailnet policy to allow funnel for this user |
| Hub cron jobs | `<config>/cron-jobs/jobs/` on the hub | the satellite's own config holds the hub-role job files, with its own paths |
| Hub-only host jobs (`<config>/cron-jobs/jobs/<hub host>/`) | hub only | not moved: they are the hub's personal jobs, not the role (Non-goals) |

No secret is copied between machines by this design.

### Bulk storage and paths

`/Volumes/local` belongs to the hub. Every hub-role job that writes there reads its root from a variable already:
`SD_DB_BACKUP_DESTINATION` and `SD_DB_BACKUP_HOURLY_DESTINATION`, plus the mirror conf's source path.
On a satellite, the hub-role job files set them to a local folder; `--require-mount` steps aside, as it does today for a named destination.
Lane logs follow `sd.bulk_storage_root`, which is already per machine.

### Satellite readiness

The standby is the work satellite (D4). `machine-setup.sh status` there prints one `hub-ready` block, read-only.
Each line is `ok`, `--` (cannot be read now, such as `sd.hub_name` with the hub down) or `NOT-READY`.
`NOT-READY` is not a drift word: a satellite that is no standby has not drifted, so the nightly drift job leaves the block out.
Slice 4 checks `hub.json`, `sd.hub_name`, the backup roots, the hub-role plists and job files, the hourly job's local root and free disk.
The pins, `gh` push access and notify lines follow with `--undo` (sd:4048). The lines:

- the system and pack checkouts at the hub's `hub-pin` (what `follow` keeps), and the pack's `sd_db` matches the hub's build (the satellite stage's proof line);
- the NAS share mounted, and both backup roots readable;
- `sd.hub_name` set, and equal to the name in `hub.json`;
- the hub-role plists and job files present and rendering;
- the active `gh` login has push access to every NULL-host repository and to every repository whose lane this machine hosts;
- the notify config complete;
- free disk above twice the newest snapshot.

A machine that fails a line can still be promoted by hand, but the verb's preflight refuses until each line reads `ok`.

### The work satellite as standby

The operator chose the work satellite (D4). It runs the work repositories' lanes, and while promoted it holds the personal records.
This section lists what that costs and how the records leave again.

**Lanes.** The work lanes stay on it: their `repo.lane_host` names it.
While promoted it also runs every NULL-host lane. `lane run --hosted` runs lanes one after another, so a long personal gate delays a work merge, and the reverse.
No lane moves between the two sets; the operator may move a busy lane with the existing Move lane control.

**One GitHub login for both sets.** `gh` has one active account per host, and the lanes share it.
Readiness checks push access to both sets with that one login. If two accounts are needed, the line reads `DIFFERS` and promotion refuses.
Switching `gh` accounts during a promotion is not part of this design: it would change the work lanes' identity under them.

**What is copied there.** Only what the hub role needs:

- at promotion, the restored state folder `~/.local/share/sd/`: the database, `providers.yaml`, `commands.yaml`, the journals, `executions/` and `publications/`;
- while promoted, the snapshots the hub-role jobs write to its local backup root, and the dashboard's and task-actions' state;
- before any failover, only the hub-role plists, job files and `sd.hub_name`, which hold no records.

No hub config folder, no hub secret and no hub host job is copied.
As a satellite it already reads every record over the wire and holds the hub's `providers.yaml`; promotion adds the records at rest.

**The drill touches the records too.** The monthly drill restores a copy into a throwaway home under the system disk and deletes it when the run ends, pass or fail, as `offsite-verify` does.
A drill killed before its cleanup leaves the folder; `status` names it, and the next drill and `promote --undo` remove it. `status` stays read-only.

**How the records leave.** The role goes back to a hub on a personal machine as soon as one is ready; the work satellite is a stopgap.
`machine-setup.sh promote --undo` on the work satellite, in order:

1. Stop the hub-role agents and jobs, then write one last snapshot to the NAS hourly root. Its checkpoint id is the hand-back point.
   Put `hub.json` back and give the tailnet name back to this machine's own: beside the database, `hub.json` makes every `sd` command refuse, so no write lands after the hand-back point.
2. The repaired or replacement hub runs `promote`, which restores that snapshot, the newest, and takes the tailnet name back.
3. `--undo` continues only when three checks pass: the fence, read from the local database, reads "elsewhere"; the new hub answers a session;
   and the new hub's newest `restore` row names the hand-back snapshot and it holds that snapshot's checkpoint. That last check ties the deletion to the one snapshot that carries every record written here.
   The newest row, open or not: the operator may run `sd restore resume` on the new hub first.
4. Delete only what undo can name as hub-role output, under `$HOME` and off the NAS: `sd.db` and its `-wal`, `-shm` and `-journal` files; each snapshot folder in the hub-role jobs' local roots named as `sd-db backup` names one (`YYYY-MM-DD` or `YYYY-MM-DD.N`), or as the seed stages one (`.seeding`); a drill home that holds the drill's marker.
   Then remove each root that is empty. A root that holds anything else stays, and the run lists what it holds.
5. Run `update satellite --apply`, and prove it by the stage's dry run, as `follow` does.
6. Prove it: none of the named items is left, and no `promote-intent` marker.

The NAS copies stay on the NAS, which is where every hub keeps them.

### Demotion and return

The repaired old hub has the lost window in its old `sd.db`: every write after the last snapshot.

- **Take the role back** (the normal path, since the standby is a work machine): first make the repaired machine a satellite, then run `promote` on it from the work satellite's hand-back snapshot, with the console step reversed.
  To make it a satellite: move its old `sd.db` to `sd.db.demoted-<date>`, and copy `hub.json` from any satellite (D3 keeps it the same on all of them).
  Step 1's preflight then finds `hub.json`, as on any satellite. The demoted file stays as the record of the lost window; the operator copies any item or note back by hand.
- **Rejoin as a satellite** (only when another personal machine takes the role): move `sd.db` aside the same way, add `<profile>.satellite`, run `update satellite --apply`.

No `demote` verb is built: `promote --undo` covers the standby, and the old hub's steps are two commands in the runbook.

### Drill

`machine-setup.sh promote --drill` runs steps 1, 5, 6 and 9 against a throwaway home on the standby satellite.
It leaves `hub.json`, launchd and Tailscale alone, starts `sd-db.sh serve --loopback` on the restored copy and opens one session.
It prints the time of each step and the total, then deletes the throwaway home.
A monthly job on the standby runs it and mails a miss.

Target: under 15 minutes from the decision to the first satellite session served.
The machine's share is under 5 minutes; the console step and reading the report take the rest.

### Loss report

Step 9 prints, and records as a `comment` note on a new item `hub failover <date>`:
the snapshot used and its checkpoint time; "writes after this time are lost"; the unbacked folders by name;
the NULL-host repositories and their open pull requests; the leases released in step 6; and the path of the moved `hub.json`.

## Mechanisms

| New or changed | Replaces or reuses |
| --- | --- |
| `machine-setup.sh promote` (with `--drill` and `--undo`) | nothing; reuses the stages, `refresh_drain.py` and the follow-intent pattern |
| `sd-db.sh restore --newest ROOT...` | reuses `restore`; the copy-off-share step from `offsite-verify.py` |
| Hourly NAS mirror chained in the hourly job | reuses `mirror-sync.sh` and `offsite-verify --preflight-only`, as the nightly job does |
| `sd.hub_name`, one declared pack setting | nothing; it could later replace each `<profile>.satellite` file |
| Hub role as a state in the `agents` and `cron` stages | the profile-only test `sd_in_profile` for the hub-role labels |
| `hub-ready` block in `status` | nothing |
| The `promote-intent` marker | reuses the follow-intent pattern |

## Non-goals

- Moving the hub's personal jobs and agents (host jobs, the iCloud mirror, personal LaunchAgents). A temporary hub runs the sd role only.
- Automatic failover. The operator decides that the hub is dead; the verb does not guess from a timeout alone.
- Recovering the lost window automatically. The demoted `sd.db` holds it, and the operator reconciles by hand.
- Carrying lane queues across machines (sd:3174's portable entries may later).
- Backing up the hub's config folder off the machine; its secrets would land on the NAS. A separate item if wanted.
- A Tailscale API key or any new secret.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 preflight | nothing | the hub answers, a readiness line fails, or the snapshot is newer than the library | refuse; nothing moved | `test_step_1_refuses_while_the_hub_answers`; `test_step_1_refuses_a_hub_ready_line_that_is_not_ok`; the schema stop is the pack's `restore --newest` test |
| 1 preflight | none | a backup root cannot be read, such as the NAS while `--from` names the USB disk | one readable root with a snapshot is a path: the others read `--`; with none readable, NOT-READY and refuse | `test_a_usb_snapshot_promotes_while_the_nas_roots_cannot_be_read`, `test_no_readable_root_is_not_ready` |
| 5 restore, 9 report | `sd.db` installed | `restore --newest` could not read a root, which may hold a newer snapshot | the pack reports it and restores the newest it can read; the report names each root not read | `test_a_usb_snapshot_promotes_while_the_nas_roots_cannot_be_read` |
| 6 seed | empty folders | the NAS hourly root cannot be read, so its names of today are unknown | every readable root's names of today are held; the hold step lists the NAS again | `test_a_usb_snapshot_promotes_while_the_nas_roots_cannot_be_read` |
| hold, after 9 | marker at `step=hold` | a NAS root still cannot be listed after the report, so a name of today there is not held and the NAS copy could overwrite it | the hub serves; the marker stays, hub-ready reads NOT-READY, exit 1; the rerun lists the NAS again, holds its names and removes the marker | `test_a_usb_snapshot_promotes_while_the_nas_roots_cannot_be_read`, `test_a_paused_promotion_holds_the_nas_names_once_the_nas_is_back`, `test_a_paused_promotion_stays_paused_while_the_nas_cannot_be_listed` |
| hold, rerun | marker at `step=hold` | the hourly job took a snapshot here while its NAS copy waited, under a name the NAS hourly root holds | each hold also compares `backup-manifest.json`, which `sd-db backup` writes last, for snapshots of any day, with the NAS hourly root's (`sd-backups-hourly`) of that name, whatever the local root is called; one that differs keeps the hold and names the folder to move aside; the nightly root is no pair | `test_a_snapshot_taken_while_paused_under_a_nas_name_keeps_the_pause`, `test_a_paused_promotion_holds_the_nas_names_once_the_nas_is_back` |
| hold, rerun | marker at `step=hold` | the read of `OFFSITE_VERIFY_ROOT` fails, so no NAS root is named | a failed read refuses and the marker stays; it is not a NAS with nothing to hold | `test_a_paused_promotion_that_cannot_read_the_nas_setting_stays_paused` |
| hold, rerun | marker at `step=hold` | an hourly backup made its folder under a NAS name and has written nothing yet when the rerun looks | not closed: the window is between `sd-db backup`'s `mkdir` and its first write, once an hour, while the operator reruns by hand; a backup that loses the race to the hold fails its `mkdir` and writes nothing | none; named here |
| hold, NAS copies | none | the nightly or hourly NAS copy runs while the promotion is unfinished | `offsite-verify.py --preflight-only` refuses while the promote marker exists, at any step, so no copy writes | `test_an_unfinished_promotion_writes_no_backup` |
| 2 drain | lane locks held | a lane or gate does not finish in 45 min | refuse; locks released; nothing moved | `refresh_drain` tests with a held lock; `test_an_apply_holds_the_lanes_first` |
| 2 drain | none | the hub is dead, and an `sd` that reaches it cannot read `sd.lane_root` | the promote drain reads the setting from this machine's pack config through the pack's `file_setting`, never through `sd`; a failed read refuses, nothing moved | `test_the_drain_reads_the_lane_root_here_while_sd_cannot_reach_the_dead_hub`, `test_a_lane_root_that_cannot_be_read_here_refuses_and_nothing_moves` |
| 2 drain | ship locks held | a ship or gate run by hand holds no lane lock | the drain waits for the gates from before, holders and those queued then, and takes every satellite ship lock, held through the role move; past 45 min it refuses, nothing moved | `test_the_drain_waits_for_a_gate_from_before`, `test_the_drain_waits_for_a_gate_queued_before_it`, `test_a_held_ship_lock_stops_the_drain_and_nothing_moves` |
| 1 preflight | none | `sd config get sd.hub_name` goes to the dead hub | bounded at 30 s; no answer reads `--`, not NOT-READY; step 7 reads the restored copy | `test_a_hub_name_the_dead_hub_cannot_give_is_unread_not_refused` |
| 3 marker | marker written | killed after the write | next run reads the marker and continues at step 4 | `test_killed_after_the_marker_a_rerun_moves_hub_json_and_goes_on` |
| 3 marker | marker written | killed mid-write | one write and a rename, so a marker always names its step; a marker with no step is deleted and step 1 runs again | `test_killed_inside_step_3_a_marker_with_no_step_starts_again` |
| 4 role | `hub.json` moved aside | killed after the move | next run continues from the marker; `status` reports the moved file and the marker | `test_killed_after_the_move_a_rerun_restores`; after the restore, `test_killed_after_the_restore_a_rerun_seeds_without_a_second_restore` |
| 5 restore | `sd.db` installed, hold row open | a candidate is refused | try the next; none left: put `hub.json` back, delete the marker, exit 1 | pack `restore --newest` tests; `test_a_restore_that_restores_nothing_puts_hub_json_back` |
| 5 restore | SQLite copy in progress | killed mid-copy | SQLite's backup API rolls back; the marker names the candidate; the rerun restores it again | kill test inside `_install_restore` (existing pattern) |
| 6 settle | leases released | killed before commit | one transaction; the rerun releases them | test with an open lease in the snapshot |
| 7 name | none | the fence prints holder but exits non-zero | a failed probe is no answer: step 7 does not pass; the rerun asks again | `test_a_fence_that_says_holder_but_fails_is_no_holder` |
| 7 name | tailnet name set | a peer holds the name | stop and print the console step; no hub-role service is installed; the rerun continues at step 7 | `test_a_peer_holding_the_name_stops_at_step_7_with_the_console_step` |
| 8 role install | none | the fence cannot be read during the stages, which skip the role and exit 0 | step 8 probes the fence again and checks each role plist before it moves the marker; the rerun installs the role | `test_a_fence_that_fails_during_the_stages_stops_at_step_8` |
| 8 role install | plists written | a bootstrap fails: the cron stage logs it and exits 0, with the plist already written | every run at step 9 asks `launchctl print` for each role label before the session; 113 sends the marker back to step 8, whose cron stage loads it (UNLOADED); any other exit is no answer, refuses and leaves step 9 to ask again | `test_a_backup_job_launchd_did_not_load_stops_at_step_8`, `test_a_launchd_that_does_not_answer_stops_before_the_session` |
| 9 rerun | marker at 9 or `hold` | killed after step 8, then a reboot drops a role label | the same check runs on every run, so the rerun goes back to step 8 | `test_a_label_launchd_lost_after_step_8_sends_the_rerun_back_to_step_8` |
| 1 preflight | none | `du` cannot read the newest snapshot's size | NOT-READY, named; the old arithmetic ended the run on an empty size | `test_a_snapshot_size_du_cannot_read_is_not_ready` |
| 8 role install | agents and jobs installed | a stage fails part way | stages converge; the rerun installs the rest | `test_a_stage_that_fails_part_way_stops_at_step_8_and_a_rerun_installs_the_rest` |
| 7 to 8 order | none | the stages run before the fence reads "holder" | they add no hub-role label; step 8 refuses until step 7 passes | `HubRole.test_no_hub_role_label_goes_in_before_the_fence_reads_holder` |
| 9 prove | marker deleted | the session fails | exit 1, the marker stays; the rerun proves again | `test_a_failed_session_keeps_the_marker_and_a_rerun_proves_again` |
| 9 prove | none | a rerun at step 9 after another machine took the name; or a server by that name that is not this machine | step 9 asks the fence again on every run, and the session must read this machine's own restore row | `test_a_rerun_at_step_9_asks_the_fence_again`, `test_step_9_refuses_a_session_that_reaches_another_database` |
| fence | old hub returns | demoted, or Tailscale logged out | serve, NULL-host lanes, NAS copies and the `hub-pin` push refuse | pack fence tests per caller; `test_a_fenced_hub_publishes_no_pins`; `test_a_fenced_machine_writes_no_backup` |
| older state | a machine with no `sd.hub_name` | the fence cannot name the holder | reads off: serve, lanes, NAS copies and `hub-pin` run as before; `sd-db fence` reports it; promote refuses at step 7 and no hub-role label goes in | pack fence test with no setting; `test_an_unset_hub_name_in_the_restored_database_refuses_before_the_name`; `HubRole.test_no_hub_role_label_goes_in_before_the_fence_reads_holder` |
| older state | a pack without `sd-db fence` | the verb exits 2 | NAS copies and `hub-pin` refuse: no answer is not a pass; the pin moves with this change | `test_a_fenced_machine_writes_no_backup` (exit 2); `test_a_fence_that_cannot_run_refuses_the_preflight` |
| 6 seed | empty folders in the hourly root | the NAS holds earlier names of the same UTC day, and `sd-db backup` takes the first free name | each NAS name of today or later is held by an empty folder, which the additive copy and retention leave alone | `test_the_seed_holds_every_nas_name_of_the_day` |
| 6 seed | snapshot copied into the hourly root | killed mid-copy | the copy goes to `<name>.seeding` first; the rerun removes it and copies again | `test_a_seed_cut_short_is_copied_again` |
| 6 seed | stale `<name>.seeding` removed | the hourly root leads onto the NAS | `rm_target` keeps it; promote stops at step 6 and names it | `test_a_seed_left_on_the_nas_is_not_deleted` |
| hourly copy | NAS gains a directory | a promoted hub with a nearly empty local root copies | additive: the NAS history stays; only the prune deletes, by checkpoint age | mirror test with a one-directory source and a seven-directory NAS root |
| hand-back entry | old `sd.db` moved aside, `hub.json` copied | the operator skips the copy | step 1 refuses: no `hub.json`; nothing moved | `test_step_1_refuses_an_old_hub_with_no_hub_json` |
| demoted, before take-back | none | the sd stage reads the hub profile, calls `hub.json` EXTRA ("remove it") and builds a database, which stops the take-back at step 1 | a hub profile with `hub.json` and no database reads `DIFFERS ... demoted` and builds none; rejoining for good takes a satellite profile (README) | `test_a_demoted_hub_profile_says_so_and_builds_no_database` |
| hand-back 8 role install | agents and jobs installed | the repaired hub keeps its hub profile, so the role check keyed on the profile never reads holder | ownership (`hub_holder`: no `hub.json`, the database here, the fence reads holder) is apart from the extra labels (`hub_role`, a profile with no sd agent); steps 8 and 9 and hub-ready ask ownership | `test_a_hub_profile_takes_the_role_back`, `test_a_hub_profile_stopped_at_step_9_reads_as_the_holder` |
| hand-back 8 role install | none | the hub profile lacks a role label | it installs only what it lists; step 8 stops and names the profile file | `test_a_hub_profile_that_lacks_a_role_label_names_the_profile` |
| hand-back 8 role install | agents and jobs installed | the hub profile also names a satellite | the agents and cron stages skip hub-only labels only for a satellite that does not hold the role | `test_a_hub_profile_that_rejoined_as_a_satellite_takes_the_role_back` |
| any stage, the live hub | none | asking the fence on a hub profile with no satellite | the stages ask it only off such a hub, once per run | `test_the_live_hub_asks_no_fence` |
| undo entry | none | a hub profile holds the role (the repaired hub after take-back) | refuse with the demotion step; nothing moved | `test_a_hub_profile_that_holds_the_role_refuses` |
| undo 1 | role stopped, hand-back snapshot written, `hub.json` back | the snapshot fails | the role restarts; nothing deleted; exit 1 | `test_a_snapshot_that_fails_starts_the_role_again_and_deletes_nothing`; killed between the stop and the snapshot, `test_killed_after_the_stop_a_rerun_writes_the_snapshot` |
| undo 1 | role stopped | a bootout fails: `cron-jobs.sh uninstall` removes the plist and exits 0 | launchd, not the plist, says each role label stopped; a label still loaded, or a print that is not 113, takes no snapshot and restarts the role | `test_a_job_launchd_still_runs_takes_no_snapshot_and_the_role_starts_again`, `test_a_launchd_that_does_not_answer_takes_no_snapshot` |
| undo 1 | role stopped | the satellite profile lists a role label, which step 1 skipped | every role label is hub-only, so each stops and is asked whatever the profile lists | `test_a_role_label_the_satellite_profile_lists_stops_too` |
| undo 1 | role stopped | a label is loaded but its plist is gone | it is booted out by label before the snapshot | `test_a_loaded_agent_with_no_plist_is_stopped_before_the_snapshot` |
| undo 1 restart | role restarted | a stage logs a failed bootstrap and exits 0 | launchd is asked for each role label; one not loaded is named with the stage to run | `test_a_role_that_does_not_start_again_is_named` |
| undo 1 | `hub.json` back | killed after the move | the rerun keeps the snapshot: every `sd` command refuses beside the database (HubConflict), so no write followed it | `test_killed_after_hub_json_went_back_in_step_1_a_rerun_keeps_the_snapshot` |
| undo 1 | snapshot written, `hub.json` still aside | killed before the move, or `hub.json` aside again before step 4 | writes may follow the snapshot: the marker goes back to step 1 and a new snapshot is taken | `test_killed_before_hub_json_went_back_a_rerun_writes_a_new_snapshot` |
| undo 2 | tailnet name given up | killed, or `tailscale set` fails | the marker names step 2; the rerun gives up the name | `test_killed_after_hub_json_went_back_a_rerun_gives_up_the_name` |
| undo 3 | nothing | the new hub restored another snapshot, lacks the hand-back checkpoint, or does not answer; this machine still holds the name; the session reaches this machine | refuse; nothing deleted; the records stay until the checks pass | `test_a_new_hub_that_does_not_answer_deletes_nothing`, `test_a_new_hub_that_restored_another_snapshot_deletes_nothing`, `test_a_new_hub_without_the_hand_back_checkpoint_deletes_nothing`, `test_a_machine_that_still_holds_the_name_deletes_nothing`, `test_a_session_that_reaches_this_machine_deletes_nothing` |
| undo 4 | records deleted | killed mid-delete | the marker names step 4; the rerun deletes the rest; a root outside `$HOME` is never deleted | `test_killed_mid_delete_a_rerun_deletes_the_rest`, `test_a_root_outside_home_is_left` |
| undo 4 delete | each root, the drill home and the database folder | a root spelled with `..`, or behind a symlinked ancestor, lands outside `$HOME` or on the NAS | `rm_target` resolves the path with `cd -P` first; a real path not under the resolved `$HOME`, on the NAS mount or holding it, is kept and named | `test_a_root_that_climbs_out_of_home_through_dotdot_is_left`, `test_a_root_behind_a_symlinked_ancestor_on_the_nas_is_left`, `test_a_root_that_holds_the_nas_mount_is_left`, `test_a_database_folder_behind_a_symlinked_ancestor_is_not_deleted` |
| undo 4 delete | each root | a name with glob characters, spaces or a newline expands or splits into other paths, or a real path ends in a newline that `$()` strips | each root is one quoted word, never a field-split substitution; a destination with a newline stops the run before any delete; resolution keeps a trailing newline and a real path with one is kept | `test_a_root_with_glob_characters_deletes_that_root_only`, `test_a_root_with_spaces_in_its_name_is_deleted`, `test_a_root_with_a_newline_in_its_name_stops_the_run_before_any_delete`, `test_a_root_that_leads_to_a_folder_ending_in_a_newline_is_left` |
| undo 4 delete | each root | a folder on the way it cannot enter, at any depth | no answer, not gone: the run stops at step 4 and names the root | `test_a_root_it_cannot_resolve_stops_the_run` |
| undo 5 | the satellite stage applied | the stage fails, or its dry run after shows drift or no proof line | the marker names step 5; the rerun runs the stage again; `status` names it | `test_a_satellite_stage_that_fails_stops_at_step_5_and_status_names_it` |
| undo 4 named | snapshot folders, the database files, a marked drill home | a root also holds unrelated files, near-miss names (`2026-10-14.5.old`, `2026-10-14.05`), or a file named like a snapshot | only named items go; the root stays and the run lists what it holds; a root with only named items is removed | `test_unrelated_files_and_near_miss_names_in_each_root_stay`, `test_the_database_folder_keeps_what_is_not_the_database`, `test_roots_with_only_sd_output_are_removed` |
| undo 4 named | none | a drill folder without the drill's marker, such as one a drill before this change left | kept and listed; removed by hand | `test_a_drill_folder_without_the_drill_marker_is_kept` |
| drill | throwaway home written | killed before cleanup | `status` names the folder; the next drill and `promote --undo` remove it first | `test_killed_before_cleanup_status_names_the_folder_and_the_next_drill_removes_it`, `test_a_hand_back_deletes_a_drill_folder_left_here` |
| drill cleanup | throwaway home deleted | the state folder leads onto the NAS through a symlinked ancestor, or the NAS mount sits below a folder it cannot enter | `rm_target` keeps a home not under the resolved state folder, on the NAS or holding it, and one whose NAS mount it cannot place; the next drill refuses and names it | `test_a_leftover_home_behind_a_symlinked_ancestor_on_the_nas_stays`, `test_a_nas_mount_it_cannot_place_stops_the_cleanup` |
| drill cleanup | throwaway home deleted | a leftover home without the drill's marker (the drill writes it first, so one killed before it is empty) | an empty home goes by `rmdir`; any other stays and the drill refuses | `test_a_leftover_home_without_the_drill_marker_stays_and_the_drill_refuses` |
| drill preflight | none | the NAS cannot be read | as promote: one readable root, such as `--from` the USB disk, passes; with none, NOT-READY and the monthly job files a miss | `test_a_drill_passes_on_a_usb_root_while_the_nas_cannot_be_read`, `test_a_drill_with_no_readable_root_refuses` |
| rollback | system reverted below the promote slice | marker and moved `hub.json` left | the old code ignores the marker; the machine runs as a hub without the role overlay; `status` shows `hub.json` missing on a satellite profile | none new; named here |

## Slices

1. **Backup reach (system and config).** The hourly job example chains the NAS mirror; a mirror conf example; `offsite-verify` checks the hourly root's age.
   Confirm the Tailscale Service tag rule here, so D3's rejection rests on a quote.
   Leaves working: every machine as today; the NAS holds hourly snapshots.
2. **Fence (pack, after sd:2997 step 1).** `sd.hub_name`; the holder check; `hosts_lane`, `serve` start, the `offsite-verify` preflight and the `hub-pin` push call it.
   The rollout sets the setting on the hub and each satellite first; `status` reports it.
   Leaves working: the hub as today; a demoted machine fenced.
3. **Restore picks the newest (pack, `sd_db`).** `restore --newest ROOT...` and the lease settling.
   Leaves working: `restore DIR` unchanged.
4. **Promote (system).** The hub role as a state, the `promote` verb, the `hub-ready` block and the README runbook, demotion included.
   `--undo`, its readiness lines (pins, `gh` push access, notify) and the promoted satellite's nightly NAS copy follow as sd:4048.
   Leaves working: satellites as today; a promotion is possible.
5. **Drill (system and config).** `promote --drill`, the monthly job example, and the first timed run quoted in the PR body.

Slices 2 and 3 touch `sd_db`. They land in the pack once sd:2997 step 1 moves it there; before that, they wait, as its freeze rule says.
`promote` calls `sd-db.sh` by path, so sd:2997's shim keeps it working through step 4.

## Decisions

The operator decided on 2026-10-10 (sd:3256 note).

- **D1. Recovery point.** Chain an additive hourly NAS copy in the hourly job, with its own 7-day NAS prune, and keep the newest 24 hourly snapshots locally (`--keep 24`, ruling sd:3637): a 1-hour recovery point.
- **D2. Where the verb lives.** `machine-setup.sh promote`. Roles, agents, cron and Tailscale routes live there, and sd:2997 does not move it.
- **D3. How satellites find the hub.** The hub's tailnet machine name moves, with one console step per failover; `hub.json` and every URL stay.
- **D4. The standby machine.** The work satellite. It runs the work repositories' lanes, and while promoted it holds the personal records.
  "The work satellite as standby" covers the lanes, the readiness checks, what is copied there and how the records leave.
- **D5. The fence.** A machine holds the hub role only while its tailnet name equals `sd.hub_name`; unknown refuses writes that leave the machine.

## Acceptance criteria

- [ ] `machine-setup.sh promote --drill` on the standby satellite prints a total under 5 minutes, and the drill job's first run is quoted in slice 5's body.
- [ ] A drill against a snapshot whose newest directory is corrupt restores the second newest and names both.
- [ ] After slice 1, `offsite-verify` passes with an hourly NAS snapshot under 3 hours old.
- [ ] With the fence on, a machine whose tailnet name differs from `sd.hub_name` refuses `sd-db.sh serve`, `lane run` for a NULL-host repository, the NAS copy and the `hub-pin` push; each refusal names the holder.
- [ ] Each failure-table row's test stops the process at its step; the next run finishes or undoes it.
- [ ] `machine-setup.sh status` on each satellite prints the `hub-ready` block, and the work satellite's reads all `ok`.
- [ ] After a drill and after a `promote --undo` rehearsal, `find ~/.local/share/sd -name 'sd.db*'` on the work satellite prints nothing, and no drill folder remains.
- [ ] `sd-docs-lint` passes from the repository root.

## Risks

- **The console step is manual.** If the operator cannot reach the Tailscale console, D3 falls back to rewriting `hub.json` per satellite. Accepted.
- **The fence fails closed on a Tailscale outage.** A healthy hub stops NULL-host merges and NAS copies while Tailscale does not answer. Accepted: merges wait, and local backups go on.
- **An hour of writes is lost by design.** The loss report and the demoted `sd.db` make it visible, not recoverable.
- **The NAS is a single point for off-machine copies.** A dead hub and a dead NAS together leave the USB disk, or nothing. Accepted for one operator.
- **Partial mirror copies.** An hourly mirror stopped mid-directory leaves a partial snapshot on the NAS. `restore --newest` refuses it on its manifest hashes and takes the next. The evidence that ties a candidate to its pass is the manifest's own checkpoint id and hashes, checked on the copied bytes.
- **Promotion picks a stale snapshot.** Ordering by the manifest's checkpoint time, not the directory name, ties "newest" to when the data was taken. A demoted hub cannot add newer ones to the NAS, because the fence refuses its copy.
- **The funnel may need a tailnet policy grant for the new node.** Readiness cannot check the policy without an API key; the drill does not test the funnel. The first real promotion may need one policy edit.
- **Personal records on a work machine (D4).** While promoted, and briefly during each monthly drill, the work satellite holds the personal records at rest.
  Software the employer runs there, such as backup or endpoint scanning, may copy them; this design cannot see or stop that. Accepted by the operator's choice; `--undo` keeps the stay short.
- **Shared lane capacity.** Personal and work lanes run in turn on one machine while promoted, so merges in both sets slow down. Accepted for a stopgap.
- **One GitHub identity.** If the personal and work repositories ever need different `gh` accounts, readiness reads `DIFFERS` and promotion refuses until one login can push to both.
- **sd:2997 timing.** Slices 2 and 3 wait for its step 1. If sd:2997 stalls, slices 1, 4 and 5 still land; `promote` then refuses at preflight until `restore --newest` exists.
