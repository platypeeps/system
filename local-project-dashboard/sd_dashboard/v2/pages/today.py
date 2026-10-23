"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, served from server.py's chain. The old
Today, kept under /classic/ when this page took `/`, was retired in sd:3402. Status (sd:5099) is the sections `sd today`
prints: `/api/today/status` (`today_status`). Decisions moved to Decide and GitHub with Your issues to GitHub (sd:5781)."""

from __future__ import annotations

from . import Api, Page


def _status(read):
    from ... import today_status

    return today_status.document(read.connection, now=read.now)


PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    api=(Api("/api/today/status", _status),),
    # server.py serves /api/now and keeps each read's badge for the rail (sd:3446).
    count="/api/now",
)
