"""HOA (sd:2116): the water system's map, Mission export trends, open followups and alarm events, from the checkout
`hoa.conf` names. The page has no classic screen to keep: v1 had no HOA view."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import hoa_screen

    return hoa_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="HOA",
    routes=("/hoa",),
    html="hoa.html",
    item="sd:2116",
    api=(Api("/api/hoa", _read),),
)
