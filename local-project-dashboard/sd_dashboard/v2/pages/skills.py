"""Skills (sd:2123): the pack catalog with each skill's weekly use. Try, Review, Promote and Demote post routes that
stay in server.py's action_route. Re-import and Evaluate post /api/skills/intake, which runs `sd skill intake` and nothing
else (sd:3992). The Intake view reads the tracked repositories at /api/skills/intake/repos and changes them at
/api/skills/intake/tracking, through the same verb (system sd:5478). The Usage view (sd:5881, Metrics' Skills view
until then) reads /api/skills/usage. The old screen, kept under /classic/, was retired in sd:3402."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from sd_db import workflow

    from ... import skills_screen

    try:
        return skills_screen.document(read.connection, now=read.now)
    except (workflow.WorkflowError, OSError, ValueError) as problem:
        return 503, {"error": str(problem) or "the skill catalog could not be read"}


def _usage(read):
    import sqlite3

    from sd_db.errors import SdDbError

    from ... import skills_screen

    try:
        return skills_screen.usage(read.connection, now=read.now)
    except (SdDbError, sqlite3.Error, ValueError) as problem:
        return 503, {"error": str(problem) or "skill use could not be read"}


def _intake(payload, principal):
    from ... import skill_intake

    return skill_intake.intake(payload)


def _repos(read):
    from ... import skill_intake

    return skill_intake.repos()


def _tracking(payload, principal):
    from ... import skill_intake

    return skill_intake.tracking(payload)


PAGE = Page(
    section="Skills",
    routes=("/skills",),
    html="skills.html",
    item="sd:2123",
    api=(Api("/api/skills", _read), Api("/api/skills/intake", write=_intake),
         Api("/api/skills/intake/repos", _repos), Api("/api/skills/intake/tracking", write=_tracking),
         Api("/api/skills/usage", _usage)),
    count="/api/skills",
    live=True,
)
