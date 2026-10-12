"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, served from server.py's chain. The old
Today, kept under /classic/ when this page took `/`, was retired in sd:3402. Decisions (sd:3012) are this page's own:
`/api/decisions` lists them and `/api/decisions/answer` records a ruling (`decisions`)."""

from __future__ import annotations

from . import Api, Page


def _decisions(read):
    from ... import decisions

    return decisions.document(read.connection)


def _answer(payload, principal):
    from ... import decisions

    return decisions.request(payload)


PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    api=(Api("/api/decisions", _decisions), Api("/api/decisions/answer", write=_answer)),
)
