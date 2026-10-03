"""Notes (sd:2120): the last week, a day at a time: merges, items done and opened, and runner runs, from the records
Activity reads. Mail, the daily note text and quick-note storage have no reader; the page shows each as unknown."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import notes_screen

    return notes_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="Notes",
    routes=("/notes",),
    html="notes.html",
    item="sd:2120",
    api=(Api("/api/notes", _read),),
)
