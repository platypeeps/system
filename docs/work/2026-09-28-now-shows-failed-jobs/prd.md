---
title: Today's Now shows failed scheduled jobs
created: 2026-09-28
---
# PRD — Today's Now shows failed scheduled jobs

## Problem

Four launchd cron jobs failed on 2026-09-27, and Today did not show them.
Now reads three sources: repos, sessions and pull requests
(`source:local-project-dashboard/sd_dashboard/now_screen.py::document`).
A failed job appears only under Operations, Jobs, which nobody opens first.
The ui-design brief for System v2 (`products/system/README.md`, "Proposed
behaviour change") ranks failed jobs first on Now, as warnings.

## Requirements

1. Now reads a fourth source, `jobs`, through `sd_db.operations.inventory`,
   the reader the Operations Jobs area already uses. No second launchd parser.
2. A job in state `failed` is one row at rank 1: band `broken`, after a dark
   collector and before every other source's row.
3. The row names the job and the signal for a run killed for cause, or
   else its exit code, and the time its log `local-cron-jobs/logs/<job>.log`
   was last written. A missing or unreadable log says so, and the row stays.
4. The row carries the retry the Operations Jobs area sends,
   `launchctl kickstart <service>`, in its detail and in a `retry` field.
   `failed` is launchd's record of the last run, so a hand-run of
   `cron-jobs.sh run <job>` (the design's `jobs.retry`) does not clear the
   row; review of 43b6f6d found this, and sd:2015 carries the design gap.
5. A jobs read that raises is one dark row naming `jobs` and the reason; the
   other three sources still render.
6. The server passes its `operations_backend` to Now, so tests and Operations
   share one seam.

## Acceptance criteria

- [x] With one failed, one idle, one running and one interrupted job, Now
      shows exactly one job row, ranked first.
- [x] A job killed by SIGSEGV names the signal; a job with no log says "no log file".
- [x] A backend whose `names` raises gives `dark:jobs` and the other rows.
- [x] `/api/now` shows the failed job from the server's `operations_backend`.
- [x] `make check` passes.

## Out of scope

- `interrupted`, `unknown` and `unloaded` jobs. Operations lists them under
  attention; Now shows `failed` only, the state the 2026-09-27 report named.
- A Retry control on the row. The page shows the line; nothing on Now writes.

## References

- `source:local-sd-db/sd_db/operations.py::inventory`
- `source:local-sd-db/sd_db/operations.py::LaunchdBackend`
- ui-design `products/system/commands.md`, `jobs.retry`.

## Log

- 2026-09-28 created
- 2026-10-03 sd:2014 lifts the first out-of-scope line: interrupted, unloaded and unknown jobs are rows at rank 2
