"""Tasks (sd:2124): the page's rows and one item's Details, read as v1 /backlog and /item/<id> read them. The page
takes v1's status, age, active, q and page filters, which Operations > Progress linked until sd:3402 (sd:2589). A /backlog address
opens this page with its query (sd:2356). Start next (sd:5107) reads its plan at `/api/tasks/start-next` and posts
the items it showed to `/api/tasks/start-next/run` (`start_next`)."""

from __future__ import annotations

from . import Api, Page


def _rows(read):
    from ... import tasks_screen

    return tasks_screen.document(read.connection, now=read.now)


def _start_plan(read):
    from ... import start_next

    return start_next.plan(read.connection, now=read.now)


def _start(payload, principal):
    from ... import start_next

    return start_next.request(payload)


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
    api=(Api("/api/tasks", _rows), Api("/api/tasks/start-next", _start_plan), Api("/api/tasks/start-next/run", write=_start),
         Api(pattern=r"/api/tasks/[1-9][0-9]{0,18}", example="/api/tasks/1", read=_details)),
    count="/api/tasks",
    live=True,
)
