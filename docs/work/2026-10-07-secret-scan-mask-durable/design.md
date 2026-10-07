---
title: Weekly secret scan pages only on new findings
created: 2026-10-07
item: sd:1254
---
# Design — weekly secret scan pages only on new findings

All examples here are synthetic. No value, path or count from a real scan enters this record.

## 1. Make `mask --apply` safe on a live file

Today the Python step in `mask` reads a file, computes the masked bytes, then opens it `r+`, writes, and truncates.
The fix keeps the same-inode rewrite, which live sessions need, and adds two guards:

1. **Settle age.** Skip a file whose `mtime` is newer than `S4S_MASK_SETTLE_MIN` minutes, default 10.
   An active session log is busy now; the next run reaches it.
2. **Compare before write.** Record `st_size` and `st_mtime_ns` at the read.
   Re-stat the open `r+` handle just before the write; when either changed, skip the file.
   The window that remains is between that `fstat` and the `truncate`: microseconds against a session that appends seconds apart.

A skipped file prints `busy <path>` and counts toward `busy N` in the summary line:

    == masked 12 your-key value(s) + 30 pattern match(es) in 4 file(s); 1 busy

Alternatives not chosen:

| Option | Why not |
| --- | --- |
| Write a temp file and rename | New inode; a session holding the old descriptor keeps writing to the unlinked file, and its log is lost. |
| `flock` the file | Claude Code and Codex take no lock, so the lock guards nothing. |
| Mask only files older than a day | Leaves every hit from the last day paging, which is most of them. |

## 2. Mask before the weekly scan

`local-cron-jobs/examples/secret-scan-weekly.job` changes its command to:

    sh "$ROOT/../local-scan-for-secrets/scan-for-secrets.sh" mask --apply --no-prune
    sh "$ROOT/../local-scan-for-secrets/scan-for-secrets.sh" critical

`--no-prune` leaves deletion to the nightly `prune`, which already owns it, and keeps the job from removing build directories.
A mask failure does not stop the scan.
(Changed in the build: pull request 1 made `mask --apply` exit 1 on a write that fails part way.
That file's mtime is fresh, so the scan reads its hits as settling and exits 0.
The job therefore keeps the mask's exit code: a scan finding, exit 2, wins; otherwise the mask's code is the job's.)

## 3. Hits the mask pass did not reach are transient

After the mask pass, a durable hit in a mask target means one of two things: the file was busy, or a session wrote the value after the pass.
Both clear on the next run, so `critical` classifies a hit as transient when its file is a mask target modified inside the settle window.
(Changed in the build: the pattern pass prints these under their own heading, `== settling`, and counts them with the transient hits.
The your-keys pass prints them as `transient:` lines, as it does scratchpad hits.
A file `mask` excludes, such as a live tool store under `~/.codex`, is not a mask target here either.)
The rule reuses the mask target list; it adds no second list.
A hit in a mask target that is older than the window still exits 2: the mask pass should have removed it, so it is a defect to see.

`critical` without the mask pass keeps today's behaviour for every file outside the window.

`mask_files.py settling` makes the call. `critical` passes it the same target and exclusion lists `mask` uses, so the two cannot drift.
A classifier that fails marks every hit durable, so a failure pages.
(Changed in the build: `S4S_SCRATCH_ROOTS` replaces the two scratchpad roots, so the `critical` tests read a fixture and never a live scratchpad.)

## 4. The first run

The backlog is large. The operator runs it once by hand at a quiet time:

    sh local-scan-for-secrets/scan-for-secrets.sh mask --no-prune     # dry run: per-file counts
    sh local-scan-for-secrets/scan-for-secrets.sh mask --apply --no-prune

The job then keeps the logs masked week to week.

## Reuse

| Piece | Used for |
| --- | --- |
| `scan-for-secrets.sh mask` | the rewrite, its target list and its patterns |
| `TRANSIENT_PATH_RE` and the transient count in `critical` | the report-not-page class |
| `local-cron-jobs` failure push | the page, unchanged |
| nightly `prune` in `local-maintenance` | retention, unchanged |

Nothing new is added to `local-notify`; the job's existing failure push carries the page.

## Open questions

| # | Question | Recommendation |
| --- | --- | --- |
| Q1 | Mask inside the weekly job, or nightly in `local-maintenance`? | Weekly. One place, one schedule; nightly is a later item. |
| Q2 | Settle window length? | 10 minutes, `S4S_MASK_SETTLE_MIN`. |
| Q3 | Treat a recent hit in a mask target as transient? | Yes, inside the window only. |
| Q4 | First backlog run by hand or by the job? | By hand, after a dry run. |
| Q5 | Fix the append race as its own pull request first? | Yes; it is a data-loss defect in `mask` today. |

## Risks

- A key leaked into a session log is masked before anyone sees it, so its exposure leaves no page.
  The operator's acceptance covers that class; the log line counts it.
- A session idle more than 10 minutes and then resumed may append while the job runs.
  The compare-before-write guard skips the file; the next run masks it.
- A masked transcript resumed later shows `<masked:$NAME>` where the value was. Intended.
