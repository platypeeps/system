---
title: Mask rewrite writes a temp file and renames it
created: 2026-10-08
item: sd:3042
---
# PRD — mask rewrite writes a temp file and renames it

## Problem

`mask --apply` rewrote a file `r+` on the same inode, then truncated it (sd:1254, #208, #209).
Three review blockers stayed open on that shape:

1. ENOSPC or EIO mid-write leaves a file half masked; the restore path can fail too.
2. An expanding write (a short value, a long `<masked:$NAME>`) overruns the old length before it fails.
3. A session that appends between the last check and the truncate loses that line.

The settle window (`S4S_MASK_SETTLE_MIN`) shrank blocker 3 but did not close it.

## Requirements

1. A failed write, sync or rename leaves the original byte-identical and no temp file behind.
2. A file another process holds open is skipped as busy, not rewritten.
3. A byte a writer adds while `mask` runs is kept.
4. No settle window, backup store or new flag.

The failure table in `design.md` maps each requirement to a test.
