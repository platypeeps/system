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
    sd-db.sh unclaim ITEM                # sets resolved_at

(Changed in the build: `sd-db.sh release` already cuts the library tag, so the release verb is `unclaim`.)

`claim` refuses on the hub: a hub session is not a satellite, and the hub lead watches its own builders.
Both are plain row writes, so they work over the wire; neither needs a lock or a directory beside the database.

Alternatives not chosen:

| Option | Why not |
| --- | --- |
| `item.fields` key | `fields` belongs to the kind's manifest; a core fact there hides from every reader. |
| New `claim` state kind | Needs a migration and a CHECK change for a row `heartbeat` already describes. |
| Read the serve log | A log is not a record; it rotates, and it names a session, not an item. |
| Hook `sd task status` in the pack | Right end state, but a pack change; a later item after the verbs prove out. |

### Claim state across writes (review rounds 1 and 2)

Class: a claim write, replacement or compaction loses the active claim's state.
Every path that writes, replaces, resolves or deletes a claim or episode row:

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| `claim`, first | one open claim row | insert fails | one transaction; nothing written, rerun `claim` | `test_a_failed_replacement_keeps_the_old_claim` |
| `claim`, replace | old row resolved, new row open | insert fails after the resolve | resolve and insert in one transaction; the old claim stays | `test_a_failed_replacement_keeps_the_old_claim` |
| `claim`, replace | as above | a second writer replaces at once: two open claims | `BEGIN IMMEDIATE` holds the read, resolve and insert; the second writer waits | `test_a_second_writer_waits_for_the_whole_replacement` |
| `claim`, replace without `--branch` | branch | the stored branch drops, so pushed commits stop counting | carry the stored branch; an explicit `--branch` replaces it | `test_a_replacement_without_a_branch_keeps_the_stored_branch` |
| `unclaim` | claim and episode resolved | a failure between the two | one transaction; it reads its rows inside it | `test_unclaim_resolves_the_claim_and_its_episode` |
| `alert` | old episode resolved, new one open | insert fails after the resolve | one transaction; the old episode stays | `test_a_failed_watermark_replacement_keeps_the_old_episode` |
| `retention.compact_heartbeats` | resolved claim rows deleted | the open row carries an older timestamp (satellite clock behind) and is deleted | an open row ranks first, whatever its timestamp | `test_the_prune_keeps_an_open_claim_written_by_a_clock_behind` |
| `open_claims` read | none | a row `claim` did not write stops every check | the row is skipped | `test_one_malformed_claim_does_not_stop_the_others` |
| `assess`, episode check | none | a failed fetch exposes older progress, which reads as a new episode | progress at or before the watermark instant is the same episode | `test_a_dropped_signal_does_not_alert_again_on_older_progress` |

A replacement resets `since` and `host`, and sets `quiet_until` only when given: a new claim is the satellite acting, so it counts as progress.

## What counts as progress

The newest of:

1. `MAX(note.timestamp)` for the item, any kind, from any machine.
2. `item.updated_at`.
3. The committer date of `origin/<branch>`, when the claim names a branch.
   The hub runs `git fetch origin <branch>` in the repository's checkout, bounded at 60 s by a subprocess timeout.
   (Changed in the build: the logic is Python, so `lib/bounded.sh` does not apply; the bound is the same.)
   A fetch that fails or times out reads the ref the last fetch left, and the output and the alert say "not fetched"; it never fails the run.
   (Changed in review round 5: the fetch failure read no branch signal at all, and fetches had no shared budget.)

A hub note on the item counts too. The hub lead writing to the item is attention, and the alarm is for silence.

## Where the check runs

`sd-db.sh satellite-stale [--notify] [--now T]` on the hub. It refuses with `HubOnly` on a satellite.

- Without `--notify` it prints one line per open claim, `fresh`, `stale`, `quiet` or `skipped`, and sends nothing.
- `sd-db.sh satellite-stale status` exits 0 when no claim is stale, 1 when one is, and 3 when no claim is open.
  A satellite answers 3: it holds no claims to watch.
- The convention 6 declaration lives in a new folder, `local-satellite-stale/satellite-stale.sh`.
  (Changed in the build: the health-check sweep runs `<entrypoint> status`, and `sd-db.sh status` is the database report.)
  The wrapper's `status`, `check` and `run` call the verb; `run` sets `SD_NOTIFY` to `local-notify/notify.sh`.
- `status` reads branch refs as the last `run` fetched them: the sweep bounds `status` at 30 s, less than one fetch may take.
- A cron job, `local-cron-jobs/examples/satellite-stale.job`, runs the wrapper's `run` every 30 minutes.
  `cron-jobs.sh` takes no hour ranges, so the verb holds the window: `SD_SATELLITE_STALE_WINDOW`, default `7-22` local hours.
  It installs in `<config>/cron-jobs/jobs/<hub host>/` only, like `satellite-lane-run.job`.
  `JOB_TIMEOUT` is 10 minutes; "Run budget" below shows how one run fits inside it.
- `cron-jobs.sh watchdog` already flags the job if launchd stops firing it.

The health-check sweep runs nightly, too seldom for a 3-hour threshold alone; the job is the alarm, the sweep the backstop.

## Run budget (review rounds 5 and 7)

Class: slow or failed I/O uses the run budget before delivery.
Every network, subprocess and database call in one `satellite-stale.sh run`, against the job's 600 s `JOB_TIMEOUT`.
Each row is bounded in total, not per call, so no number of claims moves the first send.

| Call | Count | Bound | Total worst case | On failure or bound | Test |
| --- | --- | --- | --- | --- | --- |
| database open | 1 | SQLite busy timeout, 5 s | 5 s | run fails, exit 1 | existing CLI suite |
| claim and item reads | per claim | none needed: a WAL read takes no lock | local reads | - | existing CLI suite |
| `git fetch` | per claimed branch | `min(60 s, fetch budget left)`; none starts after `FETCH_BUDGET`, 180 s | ends by 180 s | read the stored ref; alert says "not fetched" | `test_fetches_stop_at_the_budget_and_read_stored_refs` |
| `git log` after a fetch | per fetched branch | inside its fetch's bound, and 10 s | inside the fetch row | no branch signal | `test_a_fetch_and_its_read_share_one_bound` |
| `git log` of a stored ref | per unfetched branch | `min(10 s, read budget left)`; none starts after `READ_BUDGET`, 240 s | ends by 240 s | branch not read; judged on notes and item updates; alert says "not read" | `test_fifty_hung_claims_send_before_the_budget_and_are_all_named` |
| `local-notify/notify.sh` | per new stale episode | `min(60 s, deadline left)`; none starts after `RUN_BUDGET`, 480 s | ends by 540 s | `SendFailed`, no watermark, exit 1; the next run retries | `test_a_hung_notifier_is_cut_at_its_bound`, `test_no_send_starts_after_the_run_deadline` |
| watermark write | per sent alert, after its send | SQLite busy timeout, 5 s | the last ends by 545 s | run fails, exit 1; the next run alerts again | existing CLI suite |

The budgets are constants in `sd_db/satellite_stale.py`; `test_the_budgets_fit_inside_the_cron_limit` checks their order against `JOB_TIMEOUT` in the example job.
The first send starts by 240 s whatever the number of claims; every claim is assessed and named.
`status` makes no fetch, and its reads stop at `STATUS_BUDGET`, 20 s, inside the health check's 30 s bound (`test_status_reads_end_inside_the_health_check_bound`).

## How the alert is delivered

`local-notify/notify.sh -t "Satellite stalled: sd:<item>" -k status -F -c ntfy,email -b`, the health check's shape.
The body names the item, title, host, branch, the newest signal and its age, and the two silencing commands.
A delivery failure exits 1, so the cron failure push covers a lost alert.

## One alert per episode

After an alert, the run writes a `watermark` row keyed `satellite-stale:<item>` whose body is the progress timestamp it alerted on.
The next run alerts again only when the item's newest progress is later than that timestamp and stale again.
`unclaim` resolves the watermark with the claim. A send that fails writes no watermark, so the next run retries it.

## How a false alarm is silenced

- `sd-db.sh claim ITEM --quiet-until 2026-10-08T09:00` sets `quiet_until`; the claim reads `quiet` until then.
- `sd-db.sh unclaim ITEM` ends the claim when the work moved or ended.
- Any note on the item restarts the clock: `sd task note ITEM --body "paused: waiting on X"`.
- Moving the item to `blocked` stops the check for it.

## Reuse

| Piece | Used for |
| --- | --- |
| `state` table, kinds `heartbeat` and `watermark` | claim and episode records, no migration |
| `retention.compact_heartbeats` | keeps one row per key, an open row first, so the open claim survives a satellite clock behind |
| `local-notify` | ntfy and email delivery |
| `local-cron-jobs` and its watchdog | schedule and job-silence detection |
| `local-health-check` status sweep | nightly backstop through convention 6 |
| `hub.read` | the satellite test behind `HubOnly` |

## Risks

- A laptop asleep overnight is a true stall by this rule. The 07:00 to 22:00 window keeps it from paging at night;
  the first morning run alerts on a claim left open, which is the intent.
- A claim nobody releases alarms once per episode until released. The alert text names `unclaim`.
- Clock skew between machines moves a signal by seconds against a 3-hour threshold; ignored.
