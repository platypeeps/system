---
title: bulkmove moves a satellite's bulk storage to the NAS by quiescing its jobs
created: 2026-11-10
item: sd:8242
---

# Design — bulkmove-quiesce

## Problem

A satellite keeps its bulk storage in a local folder, `~/repo-storage`.
The operator ruled that it moves to the NAS and the folder goes.
The first build of the move found writers by scanning process arguments.
Five review rounds each found another way to miss one: a lane root,
links, spaces, rounding, a stale marker and relative paths.

## Approach

Stop the writers instead of finding them.
On a satellite, every pack run that writes under the bulk root starts
from one of three cron jobs: `build-dispatch`, `lane-run` and
`night-build`. Each run holds its job's `flock` for as long as it runs
(`local-cron-jobs`, sd:1251), and a slot that finds the lock held skips.
The database lists every open build, draft and lane run by host. So the
stage:

0. Reads `sd.bulk_storage_root` first. When it already names the NAS
   folder, jobs write there, so the NAS holds the newer files: the stage
   only removes what is left of the local folder, with no copy. A root
   that does not read refuses; only "not set" reads as not switched.
1. Refuses while the share is unmounted or the lane root is inside the
   folder.
2. Refuses while the database shows an open run of this host.
3. Names each NAS file the copy replaces (sd:8319), then copies while
   the jobs still run.
4. Takes and holds each loaded job's lock; a lock a run holds refuses,
   and the run goes on. Then it checks for open runs again.
5. Copies what changed, then a checksum dry run must list nothing.
6. Sets `sd.bulk_storage_root` to the NAS folder and reads it back.
7. Removes the folder.

The locks live on fds of the stage's shell, so the kernel drops them on
any exit, a signal included. No job is booted out: `bootout` sends TERM
to a running job, which would kill a lane run mid-merge (sd:3317).

A satellite that holds the hub role is skipped (sd:8322).
sd:8321, a long-lived pack service holding the old root, is moot: no
process scan remains, and no run is open while the job locks are held.

Rejected: the process scan from the first build; see Problem.

The pack-min pin (`MACHINE_SETUP_BULK_PACK_MIN`) is gone. It waited for
pack sd:8275, which moved archive copies off the dispatch tick; that
change is on the pack's main, and the satellite stage already keeps the
pack at the hub's build.

## Non-goals

- Moving the hub's bulk storage.
- Making build or lane logs write to the NAS live; pack sd:8246 writes
  them locally and moves them at run end.
- A process scan of any kind.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 0 off, hub role | nothing | stage runs where it should not | `--` or `SKIP`, folder kept | `test_off_without_a_nas`, `test_a_satellite_holding_the_hub_role_is_skipped` |
| 1 mount, lane root | nothing | share unmounted; lane root inside | FAILED, folder kept, no job touched | `test_an_unmounted_nas_keeps_the_folder`, `test_a_lane_root_inside_the_folder_keeps_it` |
| 2 open runs | nothing | a run is open; database unreadable | FAILED, folder kept, no job touched | `test_an_open_run_keeps_the_folder_and_the_jobs`, `test_an_unreadable_database_keeps_the_folder` |
| 3 first copy | files on the NAS | copy fails or hits its bound | FAILED, folder kept; rsync resumes next run | `test_a_failed_first_copy_keeps_the_folder_and_the_jobs` |
| 4 job locks | locks held | a run holds a lock; launchd names no log | FAILED, folder kept, the run goes on, locks released | `test_a_job_mid_run_is_left_running_and_keeps_the_folder`, `test_a_loaded_job_with_no_log_keeps_the_folder` |
| 4 job locks | locks held | a slot fires during the copy | it finds its lock held and skips | `test_a_slot_during_the_final_copy_finds_its_lock_held` |
| 5 second check | locks held | a run opened before the locks | FAILED, folder kept, locks released | `test_a_run_that_opened_before_the_locks_keeps_the_folder` |
| 6 second copy | files on the NAS | copy fails | FAILED, folder kept, locks released | `test_a_failed_second_copy_keeps_the_folder` |
| 7 checksum | nothing | dry run lists a file | FAILED, folder kept, locks released | `test_a_file_left_after_the_copy_keeps_the_folder` |
| 8 root | `sd.bulk_storage_root` | set fails or reads back wrong | FAILED, folder kept, locks released | `test_a_root_that_does_not_switch_keeps_the_folder` |
| 0 root read | nothing | root does not read | FAILED, folder kept, nothing copied | `test_an_unreadable_root_keeps_the_folder` |
| 9 remove | local folder | removal fails part way, or the stage stops after the switch | FAILED, locks released; the next run sees the switched root and only removes the rest, never copying over newer NAS files | `test_a_failed_removal_releases_the_locks_and_a_rerun_finishes`, `test_a_rerun_after_the_switch_keeps_newer_nas_files` |
| 10 any | locks held | TERM, INT or HUP | the kernel drops the locks with the shell; no job was touched | `test_a_signal_under_the_locks_leaves_the_jobs_as_they_were` |
| all | all of the above | none | folder gone, root set, locks released; rerun says `ok` | `test_the_move_copies_switches_and_removes` |

## Acceptance criteria

- [ ] `python3 -m unittest tests.test_bulkmove` in `local-machine-setup`
  passes, 21 tests.
- [ ] `shellcheck -S warning machine-setup.sh` is clean.

## Risks

- A run started by hand, outside the three jobs, during the lock window
  is not seen until the second database check; one started after it
  writes to the old folder and is lost with it. Accepted: on a
  satellite every pack run comes from those jobs.
- While the locks are held, the satellite claims no builds; the window
  is the second copy, small after the first.
- A run that starts during the long first copy makes the lock step
  refuse; the move waits for a quiet moment on a later run.
- The open-run read imports pack internals (`build.open_runs`,
  `draft.open_runs`, the lane's `state` rows); a pack rename fails the
  read, and the stage refuses.
