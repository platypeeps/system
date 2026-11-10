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
`night-build`. The database lists every open build, draft and lane run
by host. So the stage:

1. Refuses while the share is unmounted or the lane root is inside the
   folder.
2. Refuses while the database shows an open run of this host.
3. Names each NAS file the copy replaces (sd:8319), then copies while
   the jobs still run.
4. Boots out the loaded jobs and checks for open runs again.
5. Copies what changed, then a checksum dry run must list nothing.
6. Sets `sd.bulk_storage_root` to the NAS folder and reads it back.
7. Removes the folder.
8. Loads the stopped jobs again, on every exit path, signals included.

A satellite that holds the hub role is skipped (sd:8322).
sd:8321, a long-lived pack service holding the old root, is moot: no
process scan remains, and no run is open while the jobs are stopped.

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
| 4 stop jobs | jobs booted out | a job will not go out | FAILED, folder kept, stopped jobs loaded again | `test_a_job_that_will_not_stop_keeps_the_folder_and_restarts_the_rest` |
| 5 second check | jobs stopped | a run opened before the stop | FAILED, folder kept, jobs loaded again | `test_a_run_that_opened_during_the_stop_keeps_the_folder_and_restarts` |
| 6 second copy | files on the NAS | copy fails | FAILED, folder kept, jobs loaded again | `test_a_failed_second_copy_keeps_the_folder_and_restarts` |
| 7 checksum | nothing | dry run lists a file | FAILED, folder kept, jobs loaded again | `test_a_file_left_after_the_copy_keeps_the_folder_and_restarts` |
| 8 root | `sd.bulk_storage_root` | set fails or reads back wrong | FAILED, folder kept, jobs loaded again | `test_a_root_that_does_not_switch_keeps_the_folder_and_restarts` |
| 9 remove | local folder | removal fails part way | FAILED, jobs loaded again; data is on the NAS and the root names it, so a rerun copies nothing and removes the rest | `test_a_failed_removal_restarts_and_a_rerun_finishes` |
| 10 restart | jobs loaded | bootstrap fails | FAILED; the `cron` stage reports `UNLOADED` and loads it next run | `test_a_job_that_does_not_restart_is_failed` |
| 11 any | jobs stopped | TERM, INT or HUP | the EXIT trap loads the jobs again | `test_a_signal_while_stopped_restarts_the_jobs` |
| all | all of the above | none | folder gone, root set, jobs loaded; rerun says `ok` | `test_the_move_copies_switches_removes_and_restarts` |

## Acceptance criteria

- [ ] `python3 -m unittest tests.test_bulkmove` in `local-machine-setup`
  passes, 18 tests.
- [ ] `shellcheck -S warning machine-setup.sh` is clean.

## Risks

- A run started by hand, outside the three jobs, during the stop window
  is not seen until the second database check; one started after it
  writes to the old folder and is lost with it. Accepted: on a
  satellite every pack run comes from those jobs.
- While the jobs are stopped, the satellite claims no builds; the
  window is the second copy, small after the first.
- The open-run read imports pack internals (`build.open_runs`,
  `draft.open_runs`, the lane's `state` rows); a pack rename fails the
  read, and the stage refuses.
