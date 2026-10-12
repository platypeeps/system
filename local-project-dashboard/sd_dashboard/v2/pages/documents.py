"""Documents (sd:2114): the roots and files /documents/<key>/<file> serves. The old listing, kept under /classic/ when
this page took `/documents`, was retired in sd:3402."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import documents_screen

    return documents_screen.document(now=read.now)


PAGE = Page(
    section="Documents",
    routes=("/documents",),
    html="documents.html",
    item="sd:2114",
    api=(Api("/api/documents", _read),),
)
