"""Skills (sd:2123): the pack catalog with each skill's weekly use. Try, Review, Promote and Demote post routes the old screen
posts too, so they stay in server.py's action_route. The old screen moved to /classic/skills when this page took its path; it
stays in the palette, since its "Run with agent" opens Tasks with ?skill= (sd:2590), which this page leaves to the CLI."""

from __future__ import annotations

from . import Api, Old, Page


def _read(read):
    from sd_db import workflow

    from ... import skills_screen

    try:
        return skills_screen.document(read.connection, now=read.now)
    except (workflow.WorkflowError, OSError, ValueError) as problem:
        return 503, {"error": str(problem) or "the skill catalog could not be read"}


def _classic(read):
    from ...skills_screen import render

    return render(read.connection, now=read.now, parameters=read.parameters)


PAGE = Page(
    section="Skills",
    routes=("/skills",),
    html="skills.html",
    item="sd:2123",
    api=(Api("/api/skills", _read),),
    classic={"Skills (classic)": "/classic/skills"},
    takes=(Old("/classic/skills", _classic),),
)
