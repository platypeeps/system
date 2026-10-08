---
title: Mask rewrite keeps the length and the inode
created: 2026-10-08
item: sd:3042
---
# Design — mask rewrite keeps the length and the inode

All examples here are synthetic. No value, path or count from a real scan enters this record.

The problem and requirements are in `prd.md`.

## Shape

1. Open the file read-write (read-only for a dry run) and read it.
2. Mask each match with a label of the same length: `<masked:$NAME>` or `<masked:pattern>`, padded with `*`, or all `*` when the match is shorter than the label. Record each match's span; merge spans that overlap.
3. No match, or a dry run: stop. A match in a file that did not open to write: FAILED.
4. For each span, in order: `pread` it again; if it differs from the first read, stop. Then write it with one `pwrite` of exactly that span.
5. fsync the file.

On any stop in steps 4 and 5, mask prints FAILED with the file, the reason, and the offset and length of every span not yet fully written, then exits 1.
A half-masked value no longer matches, so a second run cannot find it; the list is the only record, and the operator rotates that key.

The length never changes, so mask never truncates and never writes past the end it read.
An appender, `O_APPEND` or not, writes past that end, so no append is lost at any moment.
No temp file, rename, holder probe, settle window, sidecar file or new flag.

## Format change

A mask used to be `<masked:$NAME>` or `<masked:pattern>` at any length.
It is now padded to the match's length, so a 40-byte token reads `<masked:pattern>` and 24 `*`.
A match shorter than the label masks to `*` only and no longer names its key.

## Why not temp plus rename

The first build wrote a temp file and renamed it over the original, after a holder probe.
Review showed the gap: a session that opens the file after the last probe keeps its descriptor across the rename.
Its later writes go to the unlinked inode and are lost.
Repro on that build: `busy=False, later record kept=False`.
Same length in place has no such gap, so the probe is gone.

## Failure table

Class: a mask write fails after partial progress (review rounds: the rename, then the 4 KiB block).
Every step that writes or decides a write, from `mask_file` in `mask_files.py`:

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 open read-write | none | file or volume not writable | FAILED only with a match; nothing written | `test_an_unwritable_file_fails_only_when_it_holds_a_match` |
| 2 mask | none | value shorter or longer than its label | all `*`, or the label padded with `*` | `test_a_value_shorter_than_its_label_masks_to_stars`, `test_a_value_longer_than_its_label_keeps_the_label` |
| 2 spans | none | a match crosses a 4 KiB boundary, or two matches overlap | one span, one `pwrite` | `test_a_token_across_a_4k_boundary_is_written_by_one_pwrite`, `test_overlapping_matches_are_one_span_and_one_pwrite` |
| 4 re-read | earlier spans written | the span changed since the read, or `pread` fails | stop; list this span and every later one | `test_a_span_changed_or_unreadable_before_its_write_stops_the_file` |
| 4 pwrite | earlier spans written; this one unknown | `pwrite` raises (EIO, ENOSPC on copy-on-write) | stop; list this span and every later one | `test_a_failed_pwrite_lists_every_span_not_yet_written` |
| 4 pwrite | earlier spans written; this one half | short write | stop; list this span and every later one | `test_a_short_pwrite_lists_the_half_written_span` |
| 5 fsync | every span written, none known on disk | fsync fails | list every span | `test_a_failed_fsync_lists_every_span` |
| 4, 5 | appended bytes past the end read | a writer appends during the mask | none needed: mask never writes there | `test_a_session_that_opens_the_file_mid_mask_keeps_every_record`, `test_a_live_appender_process_keeps_every_record` |
| all | file masked | sound run | length, inode and mode kept; hard links and symlink targets see the mask; a dry run writes nothing | `test_a_mask_keeps_the_length_the_inode_and_the_mode`, `test_a_hard_link_sees_the_mask`, `test_a_symlink_masks_its_target_and_stays_a_link`, `test_a_dry_run_writes_nothing` |

## Residual risk

1. A program that rewrites the file in place between the re-read of a span and its write loses that span to mask's write. The window is two system calls wide. The README asks the operator to quit vim and open shells first.
2. A failed or short `pwrite` can leave part of a match in clear. The FAILED list names its offset and length; the next run cannot find it, so rotate that key.
3. A match whose length is shorter than its label no longer names its key.
