---
title: A RAM disk for the gate's and test runs' temp folders
created: 2026-11-04
item: sd:7092
---

# Design — ramdisk-tmp

## Problem

On the hub, fseventsd grew to about 17 GB in three days at about 100% CPU.
A 30 s root `fs_usage` counted 54,801 file-modifying calls.
Of those, 10,988 were under `$TMPDIR/sd-tests-*` and 2,140 under `$TMPDIR/sd-local-gate-*`.
The FSEvents client that holds the events is unknown.
The operator ruled (note #43493): put those folders on a small RAM disk with FSEvents logging and Spotlight off, then measure fseventsd for three days.

## Where TMPDIR comes from

| Run | Started by | TMPDIR it gets | Folder it makes there |
| --- | --- | --- | --- |
| Lane gate | `<prefix>.cron.lane-run`: `cron-jobs.sh exec lane-run`, then `sd-ship lane run --hosted` | launchd's per-user `/var/folders/<x>/<y>/T/`; the plist names none | `sd-local-gate-<pid>-<suffix>/` (pack `bin/sd_gate_cache.py`, `worktree_prefix`), with the check's `TMPDIR` at `t/` inside it |
| Review or session gate | `sd-review`, `sd gate run` in a Claude Code session | inherited: iTerm2, herdr, the shell, then Claude Code's Bash, all `/var/folders/.../T/` | the same `sd-local-gate-*` folder |
| Pack test run | `make test` or `python -m unittest` in the pack | inherited, as above | `sd-tests-<pid>-<suffix>/` (pack `tests/__init__.py`), which also holds `HOME` and the lane root |
| This repository's `make check` | `tests/check.sh` | `/tmp/system-check.*/<job>/tmp.noindex`, on purpose: a socket path under `/var/folders` passed 104 bytes | none in `$TMPDIR`; `fs_usage` showed no writes there |

Process environments read with `ps eww` on the hub on 2026-11-04 confirm the second row: `sd-check` ran with `TMPDIR=/var/folders/.../T/sd-local-gate-68721-mnag5bse/t`, under a Claude Code session.
Both folders come from Python's `tempfile`, so they follow the TMPDIR of the process that makes them.
The choice of folder lives in the pack, so the pack half is pack sd:7557.

## Approach

The system half is one machine-setup stage, `tmpdisk`, and one example LaunchAgent.

- `MACHINE_SETUP_TMPDISK_MB` in `machine-setup.env` turns the stage on; unset leaves it off.
- The stage takes its lock first, then keeps an HFS+ RAM disk at `/Volumes/sd-tmp`: `hdiutil attach -nomount ram://<MiB × 2048>`, then `diskutil erasevolume HFS+ sd-tmp <device>`.
- It writes `.fseventsd/no_log` and `.metadata_never_index` at the volume root.
- Both are read at mount time, so the stage then unmounts and mounts the volume again.
- Only then does it write `.tmpdisk-ready`. A consumer uses the disk only while that file exists, and keeps `$TMPDIR` otherwise.
- The example agent `local.system-tools.tmpdisk` runs `machine-setup.sh update tmpdisk --apply` at login, since a reboot drops the disk.
- The nightly `update --apply` repairs the disk, and `status` counts a missing one as drift.

HFS+, not APFS: on APFS the mount is a synthesized disk that `hdiutil info` does not list beside the `ram://` image, so the stage could not tie the mount to its image.

Rejected: a new tool folder. The stage and the agents stage reuse machine-setup's drift report, nightly repair and plist install.
Rejected: setting `TMPDIR` for every Claude Code session in `settings.json`. Every temp write of every session would land on a small disk, and a full disk breaks unrelated commands.
Rejected: setting `TMPDIR` in the lane-run job alone. It covers lane gates only, about 2,000 of the 13,000 writes.

New mechanisms: the `tmpdisk` stage, the `MACHINE_SETUP_TMPDISK_MB` key, and the `.tmpdisk-ready` file.
They replace nothing: no RAM disk existed. The stage replaces a hand-made disk, which a reboot would lose without a word.

### Root

Nothing needs root.
`hdiutil attach` of a `ram://` image and `diskutil erasevolume`, `unmount` and `mount` on that image run as the user who attached it.
The markers are files on a volume the user owns.
Not verified here: the brief forbids a mount on the live machine. Install step 2 below verifies it.

### Size

`MACHINE_SETUP_TMPDISK_MB=4096` (4 GiB).
Measured on 2026-11-04 from 16:40 to 16:49 MST: every 15 s, `du -sk` of each `sd-tests-*` and `sd-local-gate-*` folder in `$TMPDIR`, summed.
The peak was 646 MB, with three gates and three test runs.
A pack gate folder is about 220 MB, 144 MB of it the tree's `.venv`; the largest gated tree, the vault, tracks 121 MB.
The gate has 5 slots: 5 × 220 MB plus test runs is about 1.5 GB. 4 GiB leaves 2.7 times that.
A Rust cold build that finds no warm `CARGO_TARGET_DIR` builds `target/` in its worktree, about 4.5 GB. That does not fit; pack sd:7557 keeps such a gate off the disk.
A RAM disk should take memory only as blocks are written; that is not measured here, and the hub has 128 GB either way.

## Non-goals

- Naming the slow FSEvents client. The three-day measurement says whether this is enough.
- Moving `tests/check.sh`'s `/tmp/system-check.*`: `fs_usage` showed no writes there.
- Choosing the folder in the pack: pack sd:7557.

## Failure table

Review round 1 (0c3a84ce) found two defects in one class: a step that read disk state or acted on a device outside the lock, or on a disk the stage had not proved its own.
Class pass: every such step now runs under the lock, and each one that acts on a device names its proof.

| Step | Under the lock | Proof the disk is ours | Test |
| --- | --- | --- | --- |
| Read `hdiutil info -plist` | yes, dry runs too | n/a: a read | every case: the fakes log each call and fail one made without the lock |
| Read the boot time (`sysctl -n kern.boottime`) | yes | n/a | `test_a_record_from_an_earlier_boot_proves_nothing` |
| Read the record `<state>/tmpdisk.device` | yes | n/a | `test_stopped_after_attach_the_device_is_detached_and_made_again` |
| Read the markers and `.tmpdisk-ready` | yes | n/a | `test_stopped_after_erase_the_markers_are_written_and_mounted` |
| Detach a device with no volume | yes | the record names it, written in this boot, and it is still an attached `ram://` | `test_stopped_after_attach_…`, `test_stopped_after_unmount_…`; left alone: `test_an_unmounted_ram_disk_the_stage_did_not_make_is_left_alone`, `test_a_record_from_an_earlier_boot_proves_nothing` |
| Attach | yes | n/a: a new device | `test_absent_disk_is_missing_then_made_with_markers_before_the_last_mount` |
| Write the record | yes, right after the attach | the device the attach named | `test_a_busy_remount_fails_and_the_next_run_finishes_it` |
| Erase | yes | the device this run attached | `test_a_failed_erase_detaches_the_device` |
| Detach after a failed erase or mount | yes | the device this run attached | `test_a_failed_erase_detaches_the_device` |
| Write the markers, unmount, mount, write ready | yes | the `ram://` disk at `/Volumes/sd-tmp` is the recorded one, or holds `.tmpdisk-ready` | `test_stopped_after_the_markers_the_disk_is_mounted_again`; left alone: `test_a_ram_disk_at_the_mount_point_the_stage_did_not_make_is_left_alone` |
| Drop the record | yes | after ready, or when it proves nothing | `test_stopped_after_ready_the_record_is_dropped` |
| Take the lock | `ln -s <pid>` makes it with its pid in one step | n/a | `test_the_lock_is_one_atomic_link_to_the_pid` |
| Clear a lock | only a link naming a pid that is gone; moved aside first, and a live one goes back | n/a | `test_stopped_after_the_lock_a_dead_holder_is_cleared`, `test_a_live_lock_taken_after_the_dead_one_was_read_stays`; never: `test_a_lock_that_names_no_pid_is_never_removed`, `test_a_folder_at_the_lock_path_is_never_taken`, `test_a_live_holder_is_left_alone` |

Each stop or failure between steps, and the run after it:

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| Read `hdiutil info -plist` | none | no answer or bad plist | `FAILED`, state unknown; no attach, no detach; the next run reads again | `test_an_unreadable_disk_list_is_unknown_not_absent` |
| Take the lock | `<state>/tmpdisk.lock`, a link to the pid | another live run holds it | `SKIP` before any read; that run makes the disk | `test_a_live_holder_is_left_alone` |
| Take the lock | as above | a stopped run left it | its pid is gone, so the run moves it aside, checks it, clears it and goes on | `test_stopped_after_the_lock_a_dead_holder_is_cleared` |
| Attach | a `ram://` device, no volume | refused | `FAILED`; nothing attached | `test_a_refused_attach_fails_and_leaves_no_disk` |
| Attach, record | device and record | stopped before erase | the next run reads the recorded device as `EXTRA`, detaches it and makes the disk again | `test_stopped_after_attach_the_device_is_detached_and_made_again` |
| Attach | device, no record yet | stopped between attach and record | the device proves nothing: named and left alone for the operator | `test_an_unmounted_ram_disk_the_stage_did_not_make_is_left_alone` |
| Erase | an HFS+ volume at `/Volumes/sd-tmp`, no markers | fails | `FAILED`; the device is detached and the record dropped | `test_a_failed_erase_detaches_the_device` |
| Erase | as above | stopped before the markers | the record proves it; the next run writes them and mounts again | `test_stopped_after_erase_the_markers_are_written_and_mounted` |
| Markers, unmount | markers on disk, not yet read | volume busy, unmount fails | `FAILED`; no ready file, so no consumer takes it; the record stays; the next run mounts again | `test_a_busy_remount_fails_and_the_next_run_finishes_it` |
| Markers, unmount | as above | stopped before the unmount | the next run mounts again | `test_stopped_after_the_markers_the_disk_is_mounted_again` |
| Unmount, mount | recorded device, volume unmounted | stopped between them | detached and made again | `test_stopped_after_unmount_the_device_is_detached_and_made_again` |
| Ready, drop record | `.tmpdisk-ready` | stopped before the record goes | the next run reads `ok` and drops it | `test_stopped_after_ready_the_record_is_dropped` |
| Disk missing at login or after a reboot | none | the agent has not run yet | consumers keep `$TMPDIR`; the login agent or the nightly update makes it; a record from the earlier boot proves nothing | `test_a_record_from_an_earlier_boot_proves_nothing`; consumer fallback: pack sd:7557 |
| Disk ejected mid-run | the run's files are gone | that run fails once | later runs keep `$TMPDIR` until the next login or nightly update | the fallback: pack sd:7557 |
| Disk full | none | a write fails with no space | that run fails; the gate's cleanup and `reap_temporary` free its folder; pack sd:7557 skips a disk with too little room | pack sd:7557 |
| Two creators race | a second volume would stack as `sd-tmp 1` | the login agent meets the nightly update | the lock, taken before any read; the loser skips | `test_a_live_holder_is_left_alone` |
| Something else at `/Volumes/sd-tmp` | none | a folder, a file image, or a RAM disk not proved ours | `DIFFERS`, left alone | `test_a_folder_that_is_not_a_ram_disk_is_drift_and_kept`, `test_a_disk_image_mounted_there_is_not_a_ram_disk`, `test_a_ram_disk_at_the_mount_point_the_stage_did_not_make_is_left_alone` |
| Size changed in `machine-setup.env` | none | the live disk has the old size | `DIFFERS`, never detached: a run may be using it | `test_another_size_is_drift_and_never_detached` |

## Acceptance criteria

- [ ] `make test SUITE=machine-setup-3` passes, `test_tmpdisk` included.
- [ ] After install, `machine-setup.sh update tmpdisk` prints `ok      /Volumes/sd-tmp — RAM disk /dev/diskN, 4096 MiB, FSEvents log and Spotlight off`.
- [ ] After install, `mdutil -s /Volumes/sd-tmp` reports indexing disabled, and `.fseventsd` holds only `no_log` after a test run.
- [ ] Once pack sd:7557 lands: a hub session reads fseventsd RSS on 2026-11-08 (`ps -o rss= -p $(pgrep -x fseventsd)`). Under 500 MB closes system sd:7092; over that, it files the client hunt.

## Risks

- `.metadata_never_index` and `no_log` may not stop the slow client; then the 2026-11-08 reading is still high. We accept that: it is the ruling's experiment.
- `.fseventsd/no_log` stops fseventsd's log for the volume. It is not verified that live events skip fseventsd's memory too; the reading settles it.
- A 4 GiB disk can fill if more gates run than the measurement saw; pack sd:7557's room check covers that.
- The ok line rests on `hdiutil info`, which ties the mount at `/Volumes/sd-tmp` to a `ram://` image, and on the two marker files; `.tmpdisk-ready` ties the markers to a mount after they were written.

## Install (the lead, after merge)

1. Set `MACHINE_SETUP_TMPDISK_MB=4096` in `<config>/machine-setup/machine-setup.env`.
2. Run `local-machine-setup/machine-setup.sh update tmpdisk`, then again with `--apply`; the second must print `created /Volumes/sd-tmp`. This is the normal-user check.
3. Read `mdutil -s /Volumes/sd-tmp` and `ls -a /Volumes/sd-tmp/.fseventsd`.
4. Copy `examples/launchagents/local.system-tools.tmpdisk.plist` to `<config>/machine-setup/launchagents/` under the machine's label prefix, add the label to the hub profile's `.agent`, and run `update agents --apply`.
5. Land pack sd:7557, then start the three-day reading.
