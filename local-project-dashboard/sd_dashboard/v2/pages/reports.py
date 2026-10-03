"""Reports (sd:2121): v1's report, job and clean-preview reads. Acknowledge and Retry post routes v1 posts too, so they
stay in server.py's action_route. The old screen stays in the palette: the page has no Toolbox, Briefs, Vault, Research
or Queues view, and no attributed batch acknowledge (`--all-clean --who`)."""

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
    classic={"Reports (classic)": "/operations?area=reports"},
)
