---
title: Mask rewrite keeps the length and the inode
created: 2026-10-08
item: sd:3042
---
# PRD — mask rewrite keeps the length and the inode

## Problem

`mask --apply` rewrote a file `r+` on the same inode, then truncated it (sd:1254, #208, #209).
Three review blockers stayed open on that shape:

1. ENOSPC or EIO mid-write leaves a file half masked; the restore path can fail too.
2. An expanding write (a short value, a long `<masked:$NAME>`) overruns the old length before it fails.
3. A session that appends between the last check and the truncate loses that line.

The settle window (`S4S_MASK_SETTLE_MIN`) shrank blocker 3 but did not close it.

## Requirements

1. A byte a writer appends while `mask` runs is kept, through any descriptor, opened at any time.
2. The file length and inode never change; mask never truncates.
3. A failed write or sync says how far it got, and a second run finishes the mask.
4. No settle window, holder probe, backup store or new flag.

The failure table in `design.md` maps each requirement to a test.
