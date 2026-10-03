"""Activity (sd:2111): the 24-hour timeline, from the records the library already keeps."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import activity_screen

    return activity_screen.document(read.connection, now=read.now, jobs_backend=read.jobs)


PAGE = Page(
    section="Activity",
    routes=("/activity",),
    html="activity.html",
    item="sd:2111",
    api=(Api("/api/activity", _read),),
)
