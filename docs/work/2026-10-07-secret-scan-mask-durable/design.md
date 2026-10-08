---
title: Secret scan masks by hand and scans weekly
created: 2026-10-07
item: sd:1254
---
# Design — secret scan masks by hand and scans weekly

All examples here are synthetic. No value, path or count from a real scan enters this record.

## 1. `mask --apply` on a live file (shipped, #208 and #209)

`mask` reads a file, computes the masked bytes, then rewrites it `r+` on the same inode and truncates.
Two guards skip a file, which then counts as busy:

1. Settle age: a file modified in the last `S4S_MASK_SETTLE_MIN` minutes (10).
2. Compare before write: the size and mtime are read again on the open handle just before the write.

A window remains between that `fstat` and the truncate.
#209 restores a write cut short, such as by a full disk, without truncating.

Alternatives not chosen:

| Option | Why not |
| --- | --- |
| Write a temp file and rename | New inode; a session holding the old descriptor keeps writing to the unlinked file, and its log is lost. |
| `flock` the file | Claude Code and Codex take no lock, so the lock guards nothing. |

## 2. The weekly job scans only

The first build ran `mask --apply --no-prune` before `critical` every Monday.
The hub's lane review blocked it: a session idle past the settle window resumes and appends between the last `fstat` and the truncate.
The reviewer reproduced the lost line.
By hand, the operator picks a quiet time; unattended, nobody does.

Operator ruling 2026-10-07 (option 1): `secret-scan-weekly.job` runs `critical` only, as before this item.
Its header names the manual procedure. A test asserts the command never names `mask`.

## 3. Settling is cut

The first build added a settling class to `critical`: a hit in a mask target modified inside the settle window was reported, not paged.
It meant "the next scheduled mask removes this hit". With no scheduled mask, that claim is false.
At 07:00 it would hide only a hit written in the last ten minutes, and nothing would remove that hit later.
So the class, its classifier in `mask_files.py`, and its tests are gone. Hits read as before this item: durable, or transient for scratchpads and `~/.codex/shell_snapshots`.

## 4. Two `mask` fixes stay

Both came out of review of the first build. The manual mask needs them.

1. **No key-like export.** `mask` exited 1 before it masked anything. It now masks the known patterns and says so on stderr.
2. **No target present.** With no history, AI store or scratchpad, `mask_file_list` ran `rg`/`grep -r` with no path, which searches the current directory, `$HOME`.
   `mask --apply` then rewrote pattern matches outside its safe list, `~/repos` included.
   It now searches nothing. The literal pass is skipped with no values: an empty line is an empty pattern, which matches every file.

`S4S_SCRATCH_ROOTS` replaces the two scratchpad roots, so the `mask` tests read a fixture and never a live scratchpad.

## 5. The manual procedure

On the machine that runs the weekly job, at a quiet time:

    sh local-scan-for-secrets/scan-for-secrets.sh mask --no-prune           # dry run: per-file counts, busy list
    sh local-scan-for-secrets/scan-for-secrets.sh mask --apply --no-prune

Sessions keep echoing the exported keys into new log lines, so the Monday page returns after a mask.
That ends only when the keys leave the shell environment, which is out of scope.

## Reuse

| Piece | Used for |
| --- | --- |
| `scan-for-secrets.sh mask` and `mask_files.py` | the manual rewrite, its targets and patterns |
| `local-cron-jobs` failure push | the page, unchanged |
| nightly `prune` in `local-maintenance` | retention, unchanged |

## Rulings

| When | Ruling |
| --- | --- |
| 2026-09-30 | Clear the accepted values with `mask --apply`. |
| 2026-10-07 ~13:30 | Q1 to Q5 accepted: weekly mask in the job, 10-minute window, settling, first mask by hand, race fix first. |
| 2026-10-07 ~14:25 | Exported-value settling hits print as `transient:`; rollout after both pull requests. |
| 2026-10-07 evening | After the lane review: the job scans only; masking stays manual. Supersedes the weekly mask and settling. |
