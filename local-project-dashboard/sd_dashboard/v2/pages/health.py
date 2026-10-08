"""Health (sd:2115): worktrees, ports and branch protection. The page is at /fleet-health because
/health is the service's own check, which the runtime reads."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import health_screen

    return health_screen.document(read.connection, now=read.now, fleet=read.fleet, ports=read.ports)


PAGE = Page(
    section="Health",
    routes=("/fleet-health",),
    html="health.html",
    item="sd:2115",
    api=(Api("/api/health", _read),),
)
