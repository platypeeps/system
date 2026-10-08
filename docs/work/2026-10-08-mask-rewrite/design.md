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
2. Mask each match with a label of the same length: `<masked:$NAME>` or `<masked:pattern>`, padded with `*`, or all `*` when the match is shorter than the label.
3. No match, or a dry run: stop. A match in a file that did not open to write: FAILED.
4. For each 4 KiB block whose masked bytes differ: read the block again; if it differs from the first read, FAILED; else `pwrite` the masked block at its offset.
5. fsync the file.

The length never changes, so mask never truncates and never writes past the end it read.
An appender, `O_APPEND` or not, writes past that end, so no append is lost at any moment.
No temp file, rename, holder probe, settle window, backup store or new flag.

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

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 1 open | none | file or volume not writable | FAILED only with a match, file as it was | `test_an_unwritable_file_fails_only_when_it_holds_a_match` |
| 2 mask | none | value shorter or longer than its label | label cut to `*` or padded with `*` | `test_a_value_shorter_than_its_label_masks_to_stars`, `test_a_value_longer_than_its_label_keeps_the_label` |
| 4 write | some blocks masked | a writer appends during the mask | none needed: appends land past the end mask read | `test_a_session_that_opens_the_file_mid_mask_keeps_every_record`, `test_a_live_appender_process_keeps_every_record` |
| 4 write | some blocks masked | another program rewrote a block since the read | stop the file as FAILED; that block and later ones stay as the other program left them | `test_a_block_rewritten_by_another_program_stops_the_file` |
| 4 write, 5 fsync | some blocks masked | EIO, or ENOSPC on a copy-on-write volume | FAILED with the count of masked blocks; length unchanged; run mask again | `test_a_failed_write_or_sync_fails_the_file_and_keeps_its_length` |
| all | file masked | sound run | length, inode and mode kept; hard links and symlink targets see the mask; a dry run writes nothing | `test_a_mask_keeps_the_length_the_inode_and_the_mode`, `test_a_match_that_spans_blocks_is_masked_whole`, `test_a_hard_link_sees_the_mask`, `test_a_symlink_masks_its_target_and_stays_a_link`, `test_a_dry_run_writes_nothing` |

## Residual risk

1. A program that rewrites the file in place between the re-read of a block and its write loses to mask's write for that block. The window is two system calls wide. The README asks the operator to quit vim and open shells first.
2. A `pwrite` cut short inside one block leaves part of a match in clear. The run reports FAILED; the next run cannot see a partial value, so rotate that key.
3. A match whose length is shorter than its label no longer names its key.
