"""Health (sd:2115): worktrees, ports and branch protection. The page is at /fleet-health because
/health is the service's own check, which the runtime reads. The page reads `/api/health/summary`, every area's lamp
and the rail badge without waiting on a walker, and `/api/health/<view>`, one view's areas; `/api/health` is still
every area in one document. Its cleanups post /api/health/cleanup (sd:3393), which
`health_cleanup` checks against the row's revision before it prunes, deletes or removes anything. Its service rows'
Restart and Start post `/api/services/<label>/<action>`, the route `sd services` shares (sd:3707). Its vault mirror rows
are read-only: the page writes no vault note (sd:5134)."""

from __future__ import annotations

from . import Api, Page


def _seams(read) -> dict:
    return {"fleet": read.fleet, "ports": read.ports, "services": read.services, "served_commit": read.served_commit}


def _read(read):
    from ... import health_screen

    return health_screen.document(read.connection, now=read.now, **_seams(read))


def _part(read):
    """`/api/health/summary`, or one view's areas at `/api/health/<view>`; another name is a 404."""
    from ... import health_screen

    name = read.path.rsplit("/", 1)[1]
    if name == "summary":
        return health_screen.summary(read.connection, now=read.now, **_seams(read))
    if name not in health_screen.VIEW_AREAS:
        return 404, {"error": f"Health has no view {name!r}: it has summary, {', '.join(health_screen.VIEW_AREAS)}."}
    return health_screen.part(read.connection, name, now=read.now, **_seams(read))


def _cleanup(payload, principal):
    from ... import health_cleanup

    return health_cleanup.request(payload)


PAGE = Page(
    section="Health",
    routes=("/fleet-health",),
    html="health.html",
    item="sd:2115",
    api=(Api("/api/health", _read), Api(pattern=r"/api/health/[a-z]+", example="/api/health/summary", read=_part),
         Api("/api/health/cleanup", write=_cleanup)),
    count="/api/health/summary",
)
