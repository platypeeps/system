"""Designs (sd:2126): the ui-design checkout's drawn pages, their screenshots and the briefs with no page yet. The old
listing, kept under /classic/, was retired in sd:3402; the files themselves stay at /designs/<path>. Hide and Unhide post
/api/designs/view (sd:3447), the view state Documents' Hide writes."""

from __future__ import annotations

from datetime import datetime

from . import Api, Page


def _read(read):
    from ... import designs

    return designs.with_views(designs.ledger(now=datetime.fromisoformat(read.now.replace("Z", "+00:00"))), read.connection)


def _view(payload, principal):
    from ... import designs

    return designs.request(payload)


PAGE = Page(
    section="Designs",
    routes=("/designs",),
    html="designs.html",
    item="sd:2126",
    api=(Api("/api/designs", _read), Api("/api/designs/view", write=_view)),
    count="/api/designs",
)
