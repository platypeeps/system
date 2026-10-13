"""Review (sd:3709): the labelling queue, its own rail page (operator ruling Q1). `/api/review` lists the open batches
and their open questions; a label posts `/api/review/rule` and its undo `/api/review/withdraw`; a batch's labels download
from `/api/review/<item>/export`. Each is `sd_db.review`, the code `sd-db review` runs (`review_screen`)."""

from __future__ import annotations

from . import Api, Page

EXPORT = r"/api/review/([1-9][0-9]{0,18})/export"


def _read(read):
    from ... import review_screen

    return review_screen.document(read.connection)


def _export(read):
    import re

    from ... import review_screen

    item = int(re.fullmatch(EXPORT, read.path)[1])
    if item > review_screen.MAX_ID:
        return 400, {"error": "No such batch."}
    return review_screen.export(read.connection, item)


def _rule(payload, principal):
    from ... import review_screen

    return review_screen.rule_request(payload)


def _withdraw(payload, principal):
    from ... import review_screen

    return review_screen.withdraw_request(payload)


PAGE = Page(
    section="Review",
    routes=("/review",),
    html="review.html",
    item="sd:3709",
    api=(Api("/api/review", _read), Api(pattern=EXPORT, example="/api/review/7/export", read=_export),
         Api("/api/review/rule", write=_rule), Api("/api/review/withdraw", write=_withdraw)),
)
