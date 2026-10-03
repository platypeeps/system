"""Designs (sd:2126): the ui-design checkout's drawn pages, their screenshots and the briefs with no page yet. The old
listing moved to /classic/designs when this page took `/designs`; the files themselves stay at /designs/<path>."""

from __future__ import annotations

from datetime import datetime

from . import Api, Old, Page


def _read(read):
    from ... import designs

    return designs.ledger(now=datetime.fromisoformat(read.now.replace("Z", "+00:00")))


def _classic(read):
    from ...designs import render

    # No connection: the listing reads the ui-design checkout.
    return render(read.parameters)


PAGE = Page(
    section="Designs",
    routes=("/designs",),
    html="designs.html",
    item="sd:2126",
    api=(Api("/api/designs", _read),),
    classic={"Designs (classic)": "/classic/designs"},
    takes=(Old("/classic/designs", _classic),),
)
