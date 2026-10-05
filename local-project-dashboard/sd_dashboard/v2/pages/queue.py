"""Queue (sd:2585): each repository's ship lane, one row per item as merging, next, building, blocked or landed, read
by `queue_screen`. Up, down, top, hold and release post /api/queue/move, which runs `sd-ship lane move|hold|release`
and nothing else."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import queue_screen

    return queue_screen.document(read.connection, now=read.now)


def _move(payload, principal):
    from ... import queue_screen

    return queue_screen.move(payload)


PAGE = Page(
    section="Queue",
    routes=("/queue",),
    html="queue.html",
    item="sd:2585",
    api=(Api("/api/queue", _read), Api("/api/queue/move", write=_move)),
)
