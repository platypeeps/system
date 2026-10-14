"""Health (sd:2115): worktrees, ports and branch protection. The page is at /fleet-health because
/health is the service's own check, which the runtime reads. Its cleanups post /api/health/cleanup (sd:3393), which
`health_cleanup` checks against the row's revision before it prunes, deletes or removes anything. Its service rows'
Restart and Start post `/api/services/<label>/<action>`, the route `sd services` shares (sd:3707). Its vault mirror rows'
Render mirror posts `/api/records/render`, `sd-db records render` (sd:3977)."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import health_screen

    return health_screen.document(read.connection, now=read.now, fleet=read.fleet, ports=read.ports,
                                  services=read.services, served_commit=read.served_commit)


def _cleanup(payload, principal):
    from ... import health_cleanup

    return health_cleanup.request(payload)


def _render(payload, principal):
    """Every `db` kind's mirror, as `sd-db records render` writes it: 200 when it ran clean, else 409 with why."""
    from pathlib import Path

    if payload:
        raise ValueError("Render mirror takes no values.")

    def render(connection):
        from sd_db import records

        mirror = records.render(connection, home=Path.home())
        found = {"ok": mirror.ok, "kinds": mirror.kinds, "written": mirror.written, "reason": mirror.reason,
                 "refused": [{"path": path, "reason": reason} for path, reason in mirror.refused]}
        return (200, found) if mirror.ok else (409, {**found, "error": f"mirror not updated: {mirror.why()}"})
    return render


PAGE = Page(
    section="Health",
    routes=("/fleet-health",),
    html="health.html",
    item="sd:2115",
    api=(Api("/api/health", _read), Api("/api/health/cleanup", write=_cleanup),
         Api("/api/records/render", write=_render)),
    count="/api/health",
)
