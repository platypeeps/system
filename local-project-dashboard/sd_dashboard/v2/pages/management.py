"""Management (sd:2118): one reading of v1's repo table, fleet, runner, services and jobs reads. The classic Jobs,
Services, Repos and Sessions areas were retired in sd:3402; Protection stays in the palette as the gap matrix. The page
reads each job's log and cancels a running job, and filters and pages services (sd:3388); it has no pull and no gap
matrix."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import management_screen

    return management_screen.document(read.connection, now=read.now, fleet=read.fleet, jobs=read.jobs, services=read.services)


PAGE = Page(
    section="Management",
    routes=("/management",),
    html="management.html",
    item="sd:2118",
    api=(Api("/api/management", _read),),
)
