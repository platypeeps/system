"""Health (sd:2115): worktrees, ports and branch protection. The page is at /fleet-health because
/health is the service's own check, which the runtime reads. Its cleanups post /api/health/cleanup (sd:3393), which
`health_cleanup` checks against the row's revision before it prunes, deletes or removes anything."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import health_screen

    return health_screen.document(read.connection, now=read.now, fleet=read.fleet, ports=read.ports)


def _cleanup(payload, principal):
    from ... import health_cleanup

    return health_cleanup.request(payload)


PAGE = Page(
    section="Health",
    routes=("/fleet-health",),
    html="health.html",
    item="sd:2115",
    api=(Api("/api/health", _read), Api("/api/health/cleanup", write=_cleanup)),
)
