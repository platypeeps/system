"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, served from server.py's chain. The old
Today, kept under /classic/ when this page took `/`, was retired in sd:3402. Decisions (sd:3012) are this page's own:
`/api/decisions` lists them and `/api/decisions/answer` records a ruling (`decisions`). Status (sd:5099) is the
sections `sd today` prints: `/api/today/status` (`today_status`). GitHub (sd:5608) is the operator's pull requests and
issues: `/api/today/github` (`today_github`); a pull request's Copilot review (sd:5633) is its one write,
`/api/today/github/copilot-review`."""

from __future__ import annotations

from . import Api, Page


def _status(read):
    from ... import today_status

    return today_status.document(read.connection, now=read.now)


def _github(read):
    from ... import today_github

    return today_github.document(read.connection, now=read.now)


def _copilot(payload, principal):
    from ... import today_github

    return today_github.copilot_request(payload)


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
    api=(Api("/api/decisions", _decisions), Api("/api/decisions/answer", write=_answer),
         Api("/api/today/status", _status), Api("/api/today/github", _github),
         Api("/api/today/github/copilot-review", write=_copilot)),
    # server.py serves /api/now and keeps each read's badge for the rail (sd:3446).
    count="/api/now",
)
