"""Documents (sd:2114): the roots and files /documents/<key>/<file> serves. The old listing, kept under /classic/ when
this page took `/documents`, was retired in sd:3402. Pin, Hide and Tag post /api/documents/view (sd:3428), and so do
Rename root and Hide root (sd:3986). Publish to posts /api/documents/publish, its untick /api/documents/unpublish (sd:5489), and Publish now /api/documents/publish-now,
whose run state is read at /api/documents/publish-now/<id> (sd:5102)."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import documents_screen

    return documents_screen.document(read.connection, now=read.now)


def _view(payload, principal):
    from ... import documents_screen

    return documents_screen.request(payload)


def _publish(payload, principal):
    from ... import publishing

    return publishing.request(payload)


def _unpublish(payload, principal):
    from ... import publishing

    return publishing.unrequest(payload)


def _drain(payload, principal):
    from ... import publish_drain

    return publish_drain.request(payload)


def _drain_status(read):
    from ... import publish_drain

    return publish_drain.status(read.path.rsplit("/", 1)[1])


PAGE = Page(
    section="Documents",
    routes=("/documents",),
    html="documents.html",
    item="sd:2114",
    api=(Api("/api/documents", _read), Api("/api/documents/view", write=_view),
         Api("/api/documents/publish", write=_publish), Api("/api/documents/unpublish", write=_unpublish),
         Api("/api/documents/publish-now", write=_drain),
         Api(pattern=r"/api/documents/publish-now/drain-[A-Za-z0-9._-]+",
             example="/api/documents/publish-now/drain-x-20261018T000000Z", read=_drain_status)),
    count="/api/documents",
)
