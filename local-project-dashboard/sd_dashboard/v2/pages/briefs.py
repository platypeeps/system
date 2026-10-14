"""Briefs (sd:2112): the routine briefs the workflow database holds (sd:3459), each read on its item page."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import briefs_screen

    return briefs_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="Briefs",
    routes=("/briefs",),
    html="briefs.html",
    item="sd:2112",
    api=(Api("/api/briefs", _read),),
)
