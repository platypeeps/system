"""Contributions (sd:2113): the projection v1 renders, open rows capped, and Make task. The old screen moved to
/classic/contributions when this page took its path; it stays in the palette, since the page shows no evidence,
dependencies or notification delivery. Acknowledge posts /api/contributions/acknowledge, which v1 posts too, so it
stays in server.py's action_route."""

from __future__ import annotations

from . import Api, Old, Page


def _read(read):
    from ... import contribution_screen

    return contribution_screen.document(read.connection, now=read.now)


def _task(payload, principal):
    from sd_db import contributions

    # Make task: the task carries the row's URL as its contribution identity, as `sd task contribution add` writes it,
    # so the projection links the two after a reread and a second filing for the same URL is refused.
    values = dict(payload)
    if (set(values) != {"key", "title"} or not isinstance(values["key"], str) or not isinstance(values["title"], str)
            or not values["title"].strip()):
        raise ValueError("Provide the contribution key and a title.")
    field = next((name for prefix, name in contributions.OBSERVED.items() if values["key"].startswith(prefix)), None)
    if field is None:
        raise ValueError("Only a contribution filed on GitHub takes a task; local work already has one.")
    changes = {field: values["key"].split(":", 1)[1]}
    return lambda connection: contributions.capture(connection, title=values["title"].strip(), changes=changes, who="dashboard")


def _classic(read):
    from ...contribution_screen import render

    return render(read.connection, parameters=read.parameters)


PAGE = Page(
    section="Contributions",
    routes=("/contributions",),
    html="contributions.html",
    item="sd:2113",
    api=(Api("/api/contributions/page", _read), Api("/api/contributions/task", write=_task)),
    classic={"Contributions (classic)": "/classic/contributions"},
    takes=(Old("/classic/contributions", _classic),),
)
