"""Briefs (sd:2112): the brief notes the vault's Briefs folder holds, read by the child Resources > Briefs runs."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import briefs_screen

    return briefs_screen.document(now=read.now)


PAGE = Page(
    section="Briefs",
    routes=("/briefs",),
    html="briefs.html",
    item="sd:2112",
    api=(Api("/api/briefs", _read),),
)
