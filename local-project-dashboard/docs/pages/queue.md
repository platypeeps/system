# Queue

Queue is at `/queue` (sd:2585): each managed repository's `sd-ship lane`,
one row per item as merging, next, building, blocked, cancelled or landed.
`GET /api/queue` (`queue_screen.document`) runs `sd-ship -C <repo> lane list`
per repository, reads the builder gate logs in the lane folder, the gate slots
from `sd gate status --json` and the machine's load. A lane hosted on another
machine still gets its section, from the hub's queue. Up, down, top, hold,
release and cancel, a blocked row's Retry and Approve, a cancelled row's
Re-queue and a running row's Release post `/api/queue/move`, which runs
`sd-ship lane move|hold|release|cancel|retry` and nothing else. Each write
carries the revision, head or claim the page showed, and the verb checks it
again under the queue's lock, so a stale write is refused (409). A failure
that is not a refusal answers 502 with an unknown outcome, and the page reads
the lane again. The full rules are in `queue_screen`'s docstring.
