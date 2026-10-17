"""HOA (sd:2116): the water system's map, Mission export trends, open followups and alarm events, from the checkout
the repo table holds as `hoa`. The page has no classic screen to keep: v1 had no HOA view. Acknowledge posts
/api/hoa/alarm (sd:4967), the view state Designs' Hide writes."""

from __future__ import annotations

from . import Api, Page, place_views


def _read(read):
    from ... import hoa_screen

    return hoa_screen.document(read.connection, now=read.now)


def _ack(payload, principal):
    from ... import hoa_screen

    return hoa_screen.acknowledge(payload)


PAGE = Page(
    section="HOA",
    routes=("/hoa",),
    html="hoa.html",
    item="sd:2116",
    api=(Api("/api/hoa", _read), Api("/api/hoa/alarm", write=_ack), *place_views("hoa")),
    count="/api/hoa",
)
