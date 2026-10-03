"""Documents (sd:2114): the roots and files /documents/<key>/<file> serves. The old listing moved to
/classic/documents when this page took `/documents`; it reads a directory, not the database."""

from __future__ import annotations

from . import Api, Old, Page


def _read(read):
    from ... import documents_screen

    return documents_screen.document(now=read.now)


def _classic(read):
    from ...documents import render

    return render(read.parameters)


PAGE = Page(
    section="Documents",
    routes=("/documents",),
    html="documents.html",
    item="sd:2114",
    api=(Api("/api/documents", _read),),
    classic={"Documents (classic)": "/classic/documents"},
    takes=(Old("/classic/documents", _classic),),
)
