"""Writing (sd:2125): every writing piece by stage, with its four gate lamps and its moves. The old filtered list, kept
under /classic/, was retired in sd:3402; Stage, Correct, Park and Revive post the item routes the item page posts
too."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import writing_screen

    return writing_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="Writing",
    routes=("/writing",),
    html="writing.html",
    item="sd:2125",
    api=(Api("/api/writing", _read),),
)
