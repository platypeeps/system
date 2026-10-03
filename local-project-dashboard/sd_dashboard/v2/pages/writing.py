"""Writing (sd:2125): every writing piece by stage, with its four gate lamps and its moves. The old filtered list moved to
/classic/writing when this page took `/writing`; Stage, Correct, Park and Revive post the item routes it and the item page
post."""

from __future__ import annotations

from . import Api, Old, Page


def _read(read):
    from ... import writing_screen

    return writing_screen.document(read.connection, now=read.now)


def _classic(read):
    from ...writing_screen import render

    return render(read.connection, now=read.now, parameters=read.parameters)


PAGE = Page(
    section="Writing",
    routes=("/writing",),
    html="writing.html",
    item="sd:2125",
    api=(Api("/api/writing", _read),),
    classic={"Writing (classic)": "/classic/writing"},
    takes=(Old("/classic/writing", _classic),),
)
