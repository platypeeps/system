"""Tasks (sd:2124): the page's rows and one item's Details, read as v1 /backlog and /item/<id> read them. The page
takes v1's status, age, active, q and page filters, and Operations > Progress links here (sd:2589). The old Backlog
stays in the palette: the page has no run selection or ?skill= yet (sd:2590)."""

from __future__ import annotations

from . import Api, Page


def _rows(read):
    from ... import tasks_screen

    return tasks_screen.document(read.connection, now=read.now)


def _details(read):
    from ... import tasks_screen

    number = int(read.path.rsplit("/", 1)[1])
    if number > 9223372036854775807:
        return 404, {"error": "No such item."}
    return tasks_screen.details(read.connection, number, now=read.now)


PAGE = Page(
    section="Tasks",
    routes=("/tasks",),
    html="tasks.html",
    item="sd:2124",
    api=(Api("/api/tasks", _rows), Api(pattern=r"/api/tasks/[1-9][0-9]{0,18}", example="/api/tasks/1", read=_details)),
    classic={"Backlog (classic)": "/backlog"},
)
