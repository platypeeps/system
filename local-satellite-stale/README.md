# local-satellite-stale

Alerts the operator when an item a satellite holds shows no progress for 3 hours (sd:2918).
The design record is `docs/work/2026-10-07-satellite-staleness-alarm/`.

## How it works

1. A satellite session claims the item it works on:

       sd-db.sh claim 2918 --branch sd-2918-my-branch

   The claim is a `heartbeat` row on the hub, keyed `satellite-claim:<item>`.
   It names the host, the branch and the start time.
2. On the hub, `satellite-stale.sh run` reads every open claim.
   Progress is the newest of: a note on the item, the item's `updated_at`, and the claimed branch's head on `origin`.
3. A claim with no progress for `SD_SATELLITE_STALE_HOURS` is stale.
   The first run that sees a stale episode sends one alert through `local-notify`.
   The next alert waits until progress moves and stalls again.

Claims on `done`, `blocked` and `ready_to_send` items are skipped: they wait on someone else.

## Commands

| Command | Where | What |
| --- | --- | --- |
| `sd-db.sh claim ITEM [--branch B] [--quiet-until ISO]` | satellite | open or replace a claim |
| `sd-db.sh unclaim ITEM` | either | release the claim and close its episode |
| `satellite-stale.sh check` | hub | one line per claim; sends nothing |
| `satellite-stale.sh run` | hub | fetch branches, alert inside the window |
| `satellite-stale.sh status` | either | convention 6: 0 none stale, 1 stale, 3 nothing to check |

`status` reads branch refs as the last `run` fetched them, so it stays inside the health check's bound.
A satellite answers `status` with 3: it holds no claims to watch.

## Silencing a false alarm

- `sd-db.sh claim ITEM --quiet-until 2026-10-08T09:00` on the satellite: quiet until then.
- `sd-db.sh unclaim ITEM`: the work moved or ended.
- `sd task note ITEM --body "paused: ..."`: any note restarts the clock.
- Move the item to `blocked`.

## Configuration

Every value has a default. Change one in `$SYSTEM_TOOLS_CONFIG/satellite-stale/.env` (copy `.env.example`), or export it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SD_SATELLITE_STALE_HOURS` | `3` | hours without progress before an alert |
| `SD_SATELLITE_STALE_WINDOW` | `7-22` | local hours that may alert, start inclusive, end exclusive |
| `SD_SATELLITE_STALE_CHANNELS` | `ntfy,email` | `local-notify` channels |

## Rollout on the hub

    cp local-cron-jobs/examples/satellite-stale.job "${SYSTEM_TOOLS_CONFIG:-$HOME/.config/system}/cron-jobs/jobs/<hub host>/"
    sh local-cron-jobs/cron-jobs.sh install satellite-stale

`<hub host>` is the hub's lower-cased `hostname -s`. The job runs every 30 minutes; the verb holds the window.

## Tests

    sh local-satellite-stale/satellite-stale.sh test -v     # the wrapper
    sh local-sd-db/sd-db.sh test tests.test_satellite_stale  # the logic
