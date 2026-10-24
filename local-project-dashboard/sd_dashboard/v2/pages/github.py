"""GitHub (sd:5781, Today's GitHub and Issues views until then): Pull requests (sd:5608) are the operator's pull requests
and issues: `/api/today/github` (`today_github`); a pull request's Copilot review (sd:5633) writes through
`/api/today/github/copilot-review`, and its Merge (sd:5668) previews with a GET and copies into the lane with a POST
(`pr_merge`); Close original (sd:5669) previews its comment with a GET and closes with a POST (`pr_close`). Your issues
(sd:4628) and Intake (sd:4840) are `/api/github/issues` (`issues`); File as item posts `/api/items` with the ref.
Your issues holds the operator's GitHub issues `today_github` reads too (sd:5899), so the Pull requests view lists none."""

from __future__ import annotations

from . import Api, Page


def _github(read):
    from ... import today_github

    return today_github.document(read.connection, now=read.now)


def _issues(read):
    from ... import issues, today_github

    found = issues.with_github(issues.document(read.connection), today_github.document(read.connection, now=read.now))
    return {**found, **issues.intake(read.connection)}


def _copilot(payload, principal):
    from ... import today_github

    return today_github.copilot_request(payload)


def _merge_preview(read):
    from ... import pr_merge

    return pr_merge.preview(read.connection, read.parameters)


def _merge(payload, principal):
    from ... import pr_merge

    return pr_merge.request(payload, principal)


def _close_preview(read):
    from ... import pr_close

    return pr_close.preview(read.connection, read.parameters)


def _close(payload, principal):
    from ... import pr_close

    return pr_close.request(payload, principal)


PAGE = Page(
    section="GitHub",
    routes=("/github",),
    html="github.html",
    item="sd:5781",
    api=(Api("/api/today/github", _github), Api("/api/github/issues", _issues),
         Api("/api/today/github/copilot-review", write=_copilot),
         # Merge (sd:5668): the GET previews, the POST copies; `pr_merge` checks the preview's revision before anything moves.
         Api("/api/today/github/merge/preview", read=_merge_preview, query=True),
         Api("/api/today/github/merge", write=_merge),
         # Close original (sd:5669): the GET shows the comment, the POST posts it once and closes the pull request.
         Api("/api/today/github/close/preview", read=_close_preview, query=True),
         Api("/api/today/github/close", write=_close)),
)
