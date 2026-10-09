"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, which stays in server.py's chain because the
classic Now panel reads it too. The old Today moved to /classic/today when this page took `/`. Decisions (sd:3012) are
this page's own: `/api/decisions` lists them and `/api/decisions/answer` records a ruling (`decisions`)."""

from __future__ import annotations

from . import Api, Old, Page


def _decisions(read):
    from ... import decisions

    return decisions.document(read.connection)


def _answer(payload, principal):
    from ... import decisions

    return decisions.request(payload)


def _classic(read):
    from ... import screens

    return screens.today(read.connection, now=read.now, parameters=read.parameters)


PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    classic={"Today (classic)": "/classic/today"},
    api=(Api("/api/decisions", _decisions), Api("/api/decisions/answer", write=_answer)),
    takes=(Old("/classic/today", _classic),),
)
