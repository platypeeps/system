"""Briefs (sd:2112): the routine briefs the workflow database holds (sd:3459), each read on its item page. An adversarial
review's brief lists its recommendations from `/api/briefs/<item>/recommendations` (sd:5064, `review_screen`). Publish to
posts /api/briefs/publish, its untick /api/briefs/unpublish (sd:5489), and Publish now /api/briefs/publish-now, whose run is read at /api/briefs/publish-now/<id> (sd:5102)."""

from __future__ import annotations

from . import Api, Page

RECOMMENDATIONS = r"/api/briefs/([1-9][0-9]{0,18})/recommendations"


def _read(read):
    from ... import briefs_screen

    return briefs_screen.document(read.connection, now=read.now)


def _recommendations(read):
    import re

    from ... import review_screen

    item = int(re.fullmatch(RECOMMENDATIONS, read.path)[1])
    if item > review_screen.MAX_ID:
        return 404, {"error": "No such brief."}
    return review_screen.recommendations(read.connection, item)


def _publish(payload, principal):
    from ... import publishing

    return publishing.brief_request(payload)


def _unpublish(payload, principal):
    from ... import publishing

    return publishing.brief_unrequest(payload)


def _drain(payload, principal):
    from ... import publish_drain

    return publish_drain.brief_request(payload)


def _drain_status(read):
    from ... import publish_drain

    return publish_drain.status(read.path.rsplit("/", 1)[1])


PAGE = Page(
    section="Briefs",
    routes=("/briefs",),
    html="briefs.html",
    item="sd:2112",
    api=(Api("/api/briefs", _read),
         Api(pattern=RECOMMENDATIONS, example="/api/briefs/7/recommendations", read=_recommendations),
         Api("/api/briefs/publish", write=_publish), Api("/api/briefs/unpublish", write=_unpublish),
         Api("/api/briefs/publish-now", write=_drain),
         Api(pattern=r"/api/briefs/publish-now/drain-[A-Za-z0-9._-]+",
             example="/api/briefs/publish-now/drain-x-20261018T000000Z", read=_drain_status)),
)
