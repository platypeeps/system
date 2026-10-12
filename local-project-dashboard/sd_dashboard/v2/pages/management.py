"""Management (sd:2118): one reading of v1's repo table, fleet, runner, services and jobs reads. Jobs, Services, Repos,
Sessions and Protection stay as shared palette screens until slice 10 of sd:1333 retires them. The page reads each job's
log and cancels a running job, and filters and pages services (sd:3388); it has no pull and no gap matrix."""

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
