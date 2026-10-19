"""Management (sd:2118): one reading of v1's repo table, fleet, runner, services and jobs reads. The classic Jobs and
Repos areas were retired in sd:3402; Services, Sessions and Protection stay as shared palette screens. The page reads
each job's log and cancels a running job, and filters and pages services (sd:3388); it has no pull and no gap matrix. Land (sd:5104) commits a checkout's work per item and enqueues it."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import management_screen

    return management_screen.document(read.connection, now=read.now, fleet=read.fleet, jobs=read.jobs, services=read.services)


def _land_preview(read):
    from ... import land

    return land.preview(read.connection, read.parameters)


def _land(payload, principal):
    from ... import land

    return land.request(payload)


PAGE = Page(
    section="Management",
    routes=("/management",),
    html="management.html",
    item="sd:2118",
    api=(Api("/api/management", _read),
         # Land (sd:5104): the GET previews, the POST lands; `land` checks the preview's revision before anything moves.
         Api("/api/repos/land/preview", read=_land_preview, query=True), Api("/api/repos/land", write=_land)),
    count="/api/management",
)
