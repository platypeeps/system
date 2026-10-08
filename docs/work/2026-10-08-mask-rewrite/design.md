---
title: Mask rewrite writes a temp file and renames it
created: 2026-10-08
item: sd:3042
---
# Design — mask rewrite writes a temp file and renames it

All examples here are synthetic. No value, path or count from a real scan enters this record.

The problem and requirements are in `prd.md`.

## Shape

1. Read the file and compute the masked bytes. No match, or a dry run: stop.
2. Ask who holds the file open: `/proc` on Linux, `lsof -t` elsewhere. Another holder: skip as busy.
3. Write the masked bytes to `mkstemp` in the same folder, copy the mode, fsync, close.
4. Check again on the read handle: size, mtime, inode at the path, and holders. Any change: unlink the temp file, skip as busy.
5. `os.replace` the temp file over the original.
6. Read the old handle once more. Bytes that reached the old inode during the rename are masked and appended to the new file.
7. fsync the folder.

No backup store, no new flag, no settle window. Masking is manual, so a busy file waits for the next run.

## Why the sd:1254 objection no longer holds

The sd:1254 design rejected temp plus rename: a session holding the old descriptor writes to the unlinked file.
Step 2 now skips any file another process holds open, so no live session keeps a descriptor to the old inode.

## Failure table

| Step | State moved | Failure | Recovery | Test |
| --- | --- | --- | --- | --- |
| 2 holders | none | another process holds the file | skip as busy | `test_a_file_another_process_holds_open_is_busy_and_untouched`, `test_the_summary_counts_a_held_file_as_busy` |
| 2 holders | none | no `lsof` and no `/proc` | FAILED, exit 1, original whole | `test_no_way_to_ask_for_holders_fails_and_leaves_the_file` |
| 2 preconditions | none | file not writable, or hard-linked | FAILED only with a match | `test_an_unwritable_file_fails_only_when_it_holds_a_match`, `test_a_hard_link_is_not_split` |
| 3 mkstemp | none | read-only folder or volume | FAILED only with a match | `test_a_temp_file_that_cannot_be_made_fails_only_when_it_holds_a_match` |
| 3 write, fsync | temp file exists | ENOSPC or EIO, also on an expanding write | unlink temp, FAILED, original whole | `test_a_failed_temp_write_or_sync_leaves_the_original_whole` |
| 4 re-check | temp file exists | a writer appended or opened the file | unlink temp, skip as busy, append kept | `test_an_append_before_the_rename_makes_the_file_busy_and_is_kept`, `test_a_session_that_opens_the_file_before_the_rename_makes_it_busy` |
| 5 rename | temp file exists | `os.replace` fails | unlink temp, FAILED, original whole | `test_a_failed_rename_leaves_the_original_whole` |
| 6 late read | new file in place | bytes reached the old inode during the rename | mask and append them | `test_bytes_written_to_the_old_inode_during_the_rename_are_masked_and_kept` |
| 7 folder fsync | new file in place | EIO | FAILED, masked bytes in place, rename may not be durable | `test_a_failed_folder_sync_fails_the_run_with_the_masked_bytes_in_place` |
| all | as above | sound run | masked, mode kept, symlink kept, no temp left | `test_a_masked_file_keeps_its_mode_and_leaves_no_temp_file`, `test_a_symlink_masks_its_target_and_stays_a_link`, `test_a_dry_run_writes_nothing` |

## Residual risk

A process that opens the file after the step 4 holder check and writes after the step 6 late read writes to the unlinked inode.
That window is a few system calls wide. No lock closes it, because sessions take none.
