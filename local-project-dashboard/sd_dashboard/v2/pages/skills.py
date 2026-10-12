"""Skills (sd:2123): the pack catalog with each skill's weekly use. Try, Review, Promote and Demote post routes that
stay in server.py's action_route. The old screen, kept under /classic/, was retired in sd:3402."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from sd_db import workflow

    from ... import skills_screen

    try:
        return skills_screen.document(read.connection, now=read.now)
    except (workflow.WorkflowError, OSError, ValueError) as problem:
        return 503, {"error": str(problem) or "the skill catalog could not be read"}


PAGE = Page(
    section="Skills",
    routes=("/skills",),
    html="skills.html",
    item="sd:2123",
    api=(Api("/api/skills", _read),),
)
