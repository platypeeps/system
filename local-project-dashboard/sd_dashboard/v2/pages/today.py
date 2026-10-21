"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, served from server.py's chain. The old
Today, kept under /classic/ when this page took `/`, was retired in sd:3402. Decisions (sd:3012) are this page's own:
`/api/decisions` lists them and `/api/decisions/answer` records a ruling (`decisions`). Status (sd:5099) is the
sections `sd today` prints: `/api/today/status` (`today_status`). GitHub (sd:5608) is the operator's pull requests and
issues: `/api/today/github` (`today_github`); a pull request's Copilot review (sd:5633) writes through
`/api/today/github/copilot-review`, and its Merge (sd:5668) previews with a GET and copies into the lane with a POST
(`pr_merge`)."""

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


def _merge_preview(read):
    from ... import pr_merge

    return pr_merge.preview(read.connection, read.parameters)


def _merge(payload, principal):
    from ... import pr_merge

    return pr_merge.request(payload, principal)


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
         Api("/api/today/github/copilot-review", write=_copilot),
         # Merge (sd:5668): the GET previews, the POST copies; `pr_merge` checks the preview's revision before anything moves.
         Api("/api/today/github/merge/preview", read=_merge_preview, query=True),
         Api("/api/today/github/merge", write=_merge)),
    # server.py serves /api/now and keeps each read's badge for the rail (sd:3446).
    count="/api/now",
)
