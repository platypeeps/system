"""Places (sd:5663): a realm with a report family of its own id is a place page, at /places?realm=<id>, with Tasks,
Briefs and Reports as HOA's and Home's (`places`). The realm and family lines are the operator's, so no realm is named
here: place.js reads the id from the address and the label from /ui/sections.js, and the rail lists each realm place
(`places.realm_places`). Each view reads /api/place/<id>/<view>; an id that is not a realm id is a 404."""

from __future__ import annotations

from . import Api, Page

#: A realm id (`places.REALM_ID`).
ID = r"[a-z0-9][a-z0-9-]*"


def view(kind: str) -> Api:
    def read(read):
        from ... import places

        realm = read.path.split("/")[3]
        if realm in places.RESERVED:
            return 404, {"error": f"{realm} is not a realm id"}
        return getattr(places, kind)(read.connection, realm, now=read.now)

    return Api(read=read, pattern=rf"/api/place/{ID}/{kind}", example=f"/api/place/acme/{kind}")


PAGE = Page(
    section="Places",
    routes=("/places",),
    html="place.html",
    item="sd:5663",
    api=tuple(view(kind) for kind in ("tasks", "briefs", "reports")),
)
