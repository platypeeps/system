---
title: Satellite staleness alarm
created: 2026-10-07
item: sd:2918
---
# Design — satellite staleness alarm

## What marks an item satellite-owned: a claim row

A claim is a `state` row of kind `heartbeat`, keyed `satellite-claim:<item>`.
The kind and its index exist: the unique index covers only the key `runner`, so no migration is needed.
The body is JSON: `host` (the lower-cased `hostname -s`), `branch`, `since`, and an optional `quiet_until`.
`resolved_at` set means released.

Two new `sd-db.sh` verbs write it:

    sd-db.sh claim ITEM [--branch B]     # on a satellite; replaces an open claim on the same item
    sd-db.sh release ITEM                # sets resolved_at

`claim` refuses on the hub: a hub session is not a satellite, and the hub lead watches its own builders.
Both are plain row writes, so they work over the wire; neither needs a lock or a directory beside the database.

Alternatives not chosen:

| Option | Why not |
| --- | --- |
| `item.fields` key | `fields` belongs to the kind's manifest; a core fact there hides from every reader. |
| New `claim` state kind | Needs a migration and a CHECK change for a row `heartbeat` already describes. |
| Read the serve log | A log is not a record; it rotates, and it names a session, not an item. |
| Hook `sd task status` in the pack | Right end state, but a pack change; a later item after the verbs prove out. |

## What counts as progress

The newest of:

1. `MAX(note.timestamp)` for the item, any kind, from any machine.
2. `item.updated_at`.
3. The committer date of `origin/<branch>`, when the claim names a branch.
   The hub runs `git fetch origin <branch>` in the repository's checkout under `st_bounded` (60 s).
   A fetch that fails or times out drops this signal and says so in the output; it never fails the run.

A hub note on the item counts too. The hub lead writing to the item is attention, and the alarm is for silence.

## Where the check runs

`sd-db.sh satellite-stale [--notify] [--now T]` on the hub. It refuses with `HubOnly` on a satellite.

- Without `--notify` it prints one line per open claim, `fresh`, `stale`, `quiet` or `skipped`, and sends nothing.
- `sd-db.sh satellite-stale status` exits 0 when no claim is stale, 1 when one is, and 3 when no claim is open.
  Its `help` carries the convention 6 sentence, so `local-health-check` sweeps it with no list to edit.
- A cron job, `local-cron-jobs/examples/satellite-stale.job`, runs `--notify` every 30 minutes, 07:00 to 21:30.
  It installs in `<config>/cron-jobs/jobs/<hub host>/` only, like `satellite-lane-run.job`.
  `JOB_TIMEOUT` is 10 minutes; the git fetches are the only slow step.
- `cron-jobs.sh watchdog` already flags the job if launchd stops firing it.

The health-check sweep runs nightly, too seldom for a 3-hour threshold alone; the job is the alarm, the sweep the backstop.

## How the alert is delivered

`local-notify/notify.sh -t "Satellite stalled: sd:<item>" -k status -F -c ntfy,email -b`, the health check's shape.
The body names the item, title, host, branch, the newest signal and its age, and the two silencing commands.
A delivery failure exits 1, so the cron failure push covers a lost alert.

## One alert per episode

After an alert, the run writes a `watermark` row keyed `satellite-stale:<item>` whose body is the progress timestamp it alerted on.
The next run alerts again only when the item's newest progress is later than that timestamp and stale again.
`release` resolves the watermark with the claim.

## How a false alarm is silenced

- `sd-db.sh claim ITEM --quiet-until 2026-10-08T09:00` sets `quiet_until`; the claim reads `quiet` until then.
- `sd-db.sh release ITEM` ends the claim when the work moved or ended.
- Any note on the item restarts the clock: `sd task note ITEM --body "paused: waiting on X"`.
- Moving the item to `blocked` stops the check for it.

## Reuse

| Piece | Used for |
| --- | --- |
| `state` table, kinds `heartbeat` and `watermark` | claim and episode records, no migration |
| `lib/bounded.sh` `st_bounded` | the per-branch fetch |
| `local-notify` | ntfy and email delivery |
| `local-cron-jobs` and its watchdog | schedule and job-silence detection |
| `local-health-check` status sweep | nightly backstop through convention 6 |
| `hub.read` | the satellite test behind `HubOnly` |

## Risks

- A laptop asleep overnight is a true stall by this rule. The 07:00 to 21:30 window keeps it from paging at night;
  the first morning run alerts on a claim left open, which is the intent.
- A claim nobody releases alarms once per episode until released. The alert text names `release`.
- Clock skew between machines moves a signal by seconds against a 3-hour threshold; ignored.
