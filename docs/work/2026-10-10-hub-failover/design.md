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

1. **Preflight, no writes.** This machine is a satellite (`hub.json` exists).
   The hub does not answer on its serve port within 30 s.
   The readiness lines (below) all read `ok`.
   The newest candidate's schema is at or below this machine's library (`SchemaTooNew` otherwise).
2. **Drain.** Hold this machine's lane locks and wait for its gates, through `refresh_drain.py`, as `follow` does.
   A lane this satellite hosts keeps its ship lock under `$XDG_STATE_HOME/sd/ship-locks/`; after step 4 the lock lives beside the database, so no ship may run across the move.
3. **Intent marker.** Write `${XDG_STATE_HOME:-~/.local/state}/sd/promote-intent` atomically, as `follow-intent` does (sd:3100).
   It names the step reached and, after step 5, the snapshot chosen.
4. **Leave the satellite role.** Move `~/.config/sd/hub.json` to `hub.json.promoted-<UTC stamp>`.
   `restore` refuses with `HubOnly` while the file exists, so this step comes first.
5. **Restore.** `sd-db.sh restore --newest <roots>`: a new option on the existing verb.
   It lists every dated directory under the roots, orders them by the manifest's checkpoint time, not by name, and tries the newest first.
   It copies a candidate off the share (as `offsite-verify.py` does), then runs the whole `restore`.
   A refused candidate prints its reason, and the next one is tried.
   `restore` already migrates the staged copy, blocks queued and running assignments, and leaves the `restore` hold row open.
6. **Settle stale rows.** In one transaction, release each open `runner_lease` and note its run `abandoned at promotion`.
   A lease names a supervisor pid on the dead hub; a pid check on this machine could match an unrelated process.
7. **Install the hub role.** Run the `sd`, `cron` and `agents` stages with the hub role on (see "Hub role" below).
   That starts `sd-serve`, the dashboard and its `tailscale serve` route, `task-actions` and its funnel, the backup jobs and `offsite-verify`.
   `lane-run` already runs on every machine; `hosts_lane` now answers yes for each lane whose host is NULL.
8. **Take the hub's name.** If no tailnet peer holds the hub's name, run `tailscale set --hostname=<hub name>`.
   If the dead node still holds it, stop and print the one console step: rename or remove that node in the Tailscale admin console.
   A rerun continues here.
9. **Prove and report.** Open one tailnet session to this machine's `sd-serve` and run `sd task show 1`.
   Print the loss report (below). Delete the marker.

Dispatch stays paused until the operator reads the report and runs `sd restore resume`, the existing step after any restore.

### Backup reach and the recovery point

Recommended (decision D1): after each hourly snapshot, mirror the hourly root to the NAS share, in the same job.
This copies the nightly job's chain exactly: `sd-db.sh backup && offsite-verify.py --preflight-only && mirror-sync.sh sync`.
The mirror is exact, not additive, with its own conf and its own NAS folder, `Backup/sd-backups-hourly`.
An exact mirror keeps the NAS window equal to the disk's, so the hourly window drops from 7 to 2 days: about 36 GB on each side.
Each run moves one 783 MB directory and deletes one.
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
A satellite's `hub.json` names `hub.example.test` (the hub's MagicDNS name). After step 8, that name resolves to the promoted machine.
The dashboard origin (`https://<name>...:8443`) and the task-actions funnel URL in sent mails keep working, because both are built from the node name.
The cost is one manual console step per failover: the dead node gives up the name. No tool can do it without a Tailscale API key, a new secret.

Rejected: a Tailscale Service name. Service hosts must be tagged nodes (to confirm in slice 1), and `serve` refuses to start on a tagged node (R8 of the second-machine plan).
Rejected as default, kept as the fallback: rewrite `hub.json` on every satellite. It needs a session on each, and every URL changes.

### The fence: the old hub returns

A returning old hub has a local `sd.db` and no `hub.json`, so today it acts as the hub at once: launchd starts its jobs at boot.
Its lane run would merge from a stale queue, its backup chain would copy stale snapshots onto the NAS with names the new hub also uses,
and a later `refresh` would push `hub-pin`.

The fence (D5): a machine holds the hub role only while its own tailnet name equals the hub's name.
The hub's name is one per-machine setting, `sd.hub_name` in the pack's machine config (`sd config set sd.hub_name hub.example.test`).
After the console step, the old node is named something else, or is logged out. Its check reads "not the holder" or "unknown".

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
| `tailscale serve` (dashboard) and `funnel` (task-actions) | per node | step 7 sets both; the funnel needs the tailnet policy to allow funnel for this user |
| Hub cron jobs | `<config>/cron-jobs/jobs/` on the hub | the satellite's own config holds the hub-role job files, with its own paths |
| Hub-only host jobs (`<config>/cron-jobs/jobs/<hub host>/`) | hub only | not moved: they are the hub's personal jobs, not the role (Non-goals) |

No secret is copied between machines by this design.

### Bulk storage and paths

`/Volumes/local` belongs to the hub. Every hub-role job that writes there reads its root from a variable already:
`SD_DB_BACKUP_DESTINATION` and `SD_DB_BACKUP_HOURLY_DESTINATION`, plus the mirror conf's source path.
On a satellite, the hub-role job files set them to a local folder; `--require-mount` steps aside, as it does today for a named destination.
Lane logs follow `sd.bulk_storage_root`, which is already per machine.

### Satellite readiness

`machine-setup.sh status` on the standby satellite prints one `hub-ready` block, read-only. Each line is `ok` or a drift word:

- the system and pack checkouts at the hub's `hub-pin` (what `follow` keeps), and the pack's `sd_db` matches the hub's build (the satellite stage's proof line);
- the NAS share mounted, and both backup roots readable;
- `sd.hub_name` set, and equal to the name in `hub.json`;
- the hub-role plists and job files present and rendering;
- `gh` logged in with push access; the notify config complete;
- free disk above twice the newest snapshot.

A machine that fails a line can still be promoted by hand, but the verb's preflight refuses until each line reads `ok`.

### Demotion and return

The repaired machine has the lost window in its old `sd.db`: every write after the last snapshot.

- **Rejoin as a satellite.** Move `sd.db` to `sd.db.demoted-<date>`, add `<profile>.satellite`, run `update satellite --apply`.
  The moved file stays as the record of the lost window; the operator copies any item or note back by hand.
- **Take the role back.** Run a backup on the temporary hub, stop its role, then run `promote` on the repaired machine, with the console step reversed.
  This is the same verb, so it gets the same drill.

A `demote` verb is not built now. If the drill shows the steps are error-prone, it folds into `promote --undo`.

### Drill

`machine-setup.sh promote --drill` runs steps 1, 5, 6 and 9 against a throwaway home on the standby satellite.
It leaves `hub.json`, launchd and Tailscale alone, starts `sd-db.sh serve --loopback` on the restored copy and opens one session.
It prints the time of each step and the total.
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
| `machine-setup.sh promote` (with `--drill`) | nothing; reuses the stages, `refresh_drain.py` and the follow-intent pattern |
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
| 1 preflight | nothing | the hub answers, a readiness line fails, or the snapshot is newer than the library | refuse; nothing moved | promote test with a fake hub that answers; with a schema-27 snapshot |
| 2 drain | lane locks held | a lane or gate does not finish in 45 min | refuse; locks released; nothing moved | reuse `refresh_drain` tests with a held lock |
| 3 marker | marker written | killed after the write | next run reads the marker and continues at step 4 | kill test between 3 and 4 |
| 4 role | `hub.json` moved aside | killed after the move | next run continues from the marker; `status` reports the moved file and the marker | kill test between 4 and 5 |
| 5 restore | `sd.db` installed, hold row open | a candidate is refused | try the next; none left: put `hub.json` back, delete the marker, exit 1 | restore test with a corrupt newest and a good second; with all corrupt |
| 5 restore | SQLite copy in progress | killed mid-copy | SQLite's backup API rolls back; the marker names the candidate; the rerun restores it again | kill test inside `_install_restore` (existing pattern) |
| 6 settle | leases released | killed before commit | one transaction; the rerun releases them | test with an open lease in the snapshot |
| 7 role install | agents and jobs installed | a stage fails part way | stages converge; the rerun installs the rest | promote rerun test after a failed `agents` stage double |
| 8 name | tailnet name set | a peer holds the name | stop and print the console step; the rerun continues | test with a `tailscale status` double that lists the name on a peer |
| 9 prove | marker deleted | the session fails | exit 1, the marker stays; the rerun proves again | test with `serve` not started |
| fence | old hub returns | demoted, or Tailscale logged out | serve, NULL-host lanes, NAS copies and the `hub-pin` push refuse | one test per caller with a holder, a non-holder and an unknown `tailscale` double |
| older state | a machine with no `sd.hub_name` | the fence cannot name the holder | reads unknown: serve and NULL-host lanes refuse, and `status` names the missing setting | fence test with no setting |
| older state | the hub before slice 2 lands its setting | the hub's own lanes stop | slice 2 sets `sd.hub_name` on the hub in the same rollout, and its `status` checks it before the fence turns on | rollout step in the slice 2 body; status test |
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
   Leaves working: satellites as today; a promotion is possible.
5. **Drill (system and config).** `promote --drill`, the monthly job example, and the first timed run quoted in the PR body.

Slices 2 and 3 touch `sd_db`. They land in the pack once sd:2997 step 1 moves it there; before that, they wait, as its freeze rule says.
`promote` calls `sd-db.sh` by path, so sd:2997's shim keeps it working through step 4.

## Open decisions

The recommended option is first in each.

- **D1. Recovery point.**
  Option: chain an exact hourly NAS mirror and cut the hourly window to 2 days (recovery point 1 hour, about 36 GB on the NAS).
  Option: the same mirror, keeping the 7-day window (about 117 GB on the NAS).
  Option: build nothing; accept up to 24 hours off the hub, and 1 hour only when the USB disk survives.
- **D2. Where the verb lives.**
  Option: `machine-setup.sh promote`. Roles, agents, cron and Tailscale routes live there, and sd:2997 does not move it.
  Option: `sd-db.sh promote`. It moves to the pack with sd:2997, and would then drive launchd and cron from the pack.
- **D3. How satellites find the hub.**
  Option: the hub's tailnet machine name moves, with one console step per failover; `hub.json` and every URL stay.
  Option: rewrite `hub.json` on each satellite; every dashboard and task-actions URL changes.
- **D4. The standby machine.**
  Option: terra, as the one standby, with an exception to its setup-only ruling while it is promoted. The hub's personal records then stay off a work machine.
  Option: the work satellite. It already runs lanes, so it is closest to ready, but it would hold the personal database.
  Option: both kept ready, chosen at failure time; the readiness work doubles.
- **D5. The fence.**
  Option: the holder check by tailnet name, with the new `sd.hub_name` setting; unknown refuses writes that leave the machine.
  Option: no automatic fence; the operator keeps the old hub off the network until it is demoted by hand.

## Acceptance criteria

- [ ] `machine-setup.sh promote --drill` on the standby satellite prints a total under 5 minutes, and the drill job's first run is quoted in slice 5's body.
- [ ] A drill against a snapshot whose newest directory is corrupt restores the second newest and names both.
- [ ] After slice 1, `offsite-verify` passes with an hourly NAS snapshot under 3 hours old.
- [ ] With the fence on, a machine whose tailnet name differs from `sd.hub_name` refuses `sd-db.sh serve`, `lane run` for a NULL-host repository, the NAS copy and the `hub-pin` push; each refusal names the holder.
- [ ] Each failure-table row's test stops the process at its step; the next run finishes or undoes it.
- [ ] `machine-setup.sh status` on each satellite prints the `hub-ready` block, and the standby's reads all `ok`.
- [ ] `sd-docs-lint` passes from the repository root.

## Risks

- **The console step is manual.** If the operator cannot reach the Tailscale console, D3 falls back to rewriting `hub.json` per satellite. Accepted.
- **The fence fails closed on a Tailscale outage.** A healthy hub stops NULL-host merges and NAS copies while Tailscale does not answer. Accepted: merges wait, and local backups go on.
- **An hour of writes is lost by design.** The loss report and the demoted `sd.db` make it visible, not recoverable.
- **The NAS is a single point for off-machine copies.** A dead hub and a dead NAS together leave the USB disk, or nothing. Accepted for one operator.
- **Partial mirror copies.** An hourly mirror stopped mid-directory leaves a partial snapshot on the NAS. `restore --newest` refuses it on its manifest hashes and takes the next. The evidence that ties a candidate to its pass is the manifest's own checkpoint id and hashes, checked on the copied bytes.
- **Promotion picks a stale snapshot.** Ordering by the manifest's checkpoint time, not the directory name, ties "newest" to when the data was taken. A demoted hub cannot add newer ones to the NAS, because the fence refuses its copy.
- **The funnel may need a tailnet policy grant for the new node.** Readiness cannot check the policy without an API key; the drill does not test the funnel. The first real promotion may need one policy edit.
- **sd:2997 timing.** Slices 2 and 3 wait for its step 1. If sd:2997 stalls, slices 1, 4 and 5 still land; `promote` then refuses at preflight until `restore --newest` exists.
