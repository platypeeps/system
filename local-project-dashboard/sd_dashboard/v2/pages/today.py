"""Today (sd:2110, the default since sd:2163): /api/now as one page, served from server.py's chain. The old Today, kept
under /classic/ when this page took `/`, was retired in sd:3402. Decisions moved to Decide and GitHub with Your issues to
GitHub (sd:5781). The Status view and its /api/today/status went with the tabs (sd:5921): `sd today` prints the sections."""

from __future__ import annotations

from . import Page

PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    # server.py serves /api/now and keeps each read's badge for the rail (sd:3446).
    count="/api/now",
)
