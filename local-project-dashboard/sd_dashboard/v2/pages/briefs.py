"""Briefs (sd:2112): the routine briefs the workflow database holds (sd:3459), each read on its item page. An adversarial
review's brief lists its recommendations from `/api/briefs/<item>/recommendations` (sd:5064, `review_screen`)."""

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


PAGE = Page(
    section="Briefs",
    routes=("/briefs",),
    html="briefs.html",
    item="sd:2112",
    api=(Api("/api/briefs", _read),
         Api(pattern=RECOMMENDATIONS, example="/api/briefs/7/recommendations", read=_recommendations)),
)
