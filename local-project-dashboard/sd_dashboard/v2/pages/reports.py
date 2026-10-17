"""Reports (sd:2121): v1's report and job reads. Acknowledge and Retry post routes v1 posts too, so they stay in
server.py's action_route, beside the reopen route Acknowledge's Undo posts (sd:2395). The classic Reports area was retired
in sd:3402. The clean preview and the attributed batch acknowledge left the page: since the quiet-run rule a clean run
files no report, so `sd reports acknowledge --all-clean` stays a CLI verb only."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import reports_screen

    return reports_screen.document(read.connection, now=read.now, jobs=read.jobs)


PAGE = Page(
    section="Reports",
    routes=("/reports",),
    html="reports.html",
    item="sd:2121",
    api=(Api("/api/reports", _read),),
    count="/api/reports",
)
