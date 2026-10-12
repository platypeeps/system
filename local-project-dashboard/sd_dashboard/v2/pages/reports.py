"""Reports (sd:2121): v1's report, job and clean-preview reads. Acknowledge, Retry and the attributed batch acknowledge
(`--all-clean --who`, sd:3386) post routes v1 posts too, so they stay in server.py's action_route, beside the reopen route
Acknowledge's Undo posts (sd:2395). The classic Reports area was retired in sd:3402; Toolbox, Vault and Queues stay
on the classic Resources area, which the Research page keeps in the palette."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import reports_screen

    return reports_screen.document(read.connection, now=read.now, jobs=read.jobs)


def _clean(read):
    from sd_db import workflow

    from ... import reports_screen

    if set(read.parameters) != {"before"} or len(read.parameters["before"]) != 1:
        return 400, {"error": "Give one date: /api/reports/clean?before=YYYY-MM-DD."}
    try:
        return reports_screen.clean(read.connection, read.parameters["before"][0], now=read.now)
    except workflow.WorkflowError as problem:
        return 400, {"error": str(problem)}


PAGE = Page(
    section="Reports",
    routes=("/reports",),
    html="reports.html",
    item="sd:2121",
    api=(Api("/api/reports", _read), Api("/api/reports/clean", _clean, query=True)),
)
