---
title: Weekly secret scan pages only on new findings
created: 2026-10-07
item: sd:1254
---
# PRD — weekly secret scan pages only on new findings

## Problem

The weekly `critical` scan exits 2 every Monday, and the cron job turns exit 2 into a phone push.
The hits that cause it are key values inside AI session logs under `~/.claude/projects` and `~/.codex/sessions`.
The operator accepted those values on 2026-09-21 (`local-scan-for-secrets/README.md`, "Accepted exposure").
An alarm that fires every week on accepted values hides the one week it fires on a new leak.

On 2026-09-30 the operator ruled: run `mask --apply` on the durable hits, so the alarm fires only on new ones.
Two facts stop a one-time run from doing that:

1. Sessions keep writing the same exported values into new log lines.
   A log masked on Sunday holds new hits by Monday, so a one-time mask silences one Monday only.
2. `mask --apply` can lose data in a live log.
   It reads the whole file, then opens it `r+`, writes the masked bytes, and truncates.
   A line a session appends between the read and the truncate is lost.

## Goal

The Monday page fires only for a hit that `mask` cannot remove: a value outside the session logs, shell history and scratchpads.

## Requirements

1. Masking never loses a byte that a live session appended.
2. The weekly job masks the safe targets before it scans.
3. A hit in a mask target that the mask pass could not settle is reported, not paged.
   The next run masks it.
4. A durable hit anywhere else still exits 2 and pages, as today.
5. The job log records how many values each run masked and how many files it skipped as busy.
6. The first mask of the backlog runs by hand, after a dry run the operator reads.

## Acceptance criteria

- [ ] A file that grows between the read and the write keeps every appended byte, and the run counts it as busy.
- [ ] A file modified inside the settle window is skipped and counted as busy.
- [ ] After the job, a hit in a session log reads `transient` and the run exits 0.
- [ ] A hit in a synthetic `~/repos` file still exits 2.
- [ ] The job log line names the masked count and the busy count.
- [ ] `make check` passes.

## Out of scope

- Stopping secrets from entering the shell environment that sessions echo; that is the root cause.
- Rotating keys; the operator accepted them.
- Masking in the nightly maintenance run; a later item if the weekly cadence proves too slow.

## Log

- 2026-10-07 design record written on a satellite; open questions sent to the hub lead for the operator.
