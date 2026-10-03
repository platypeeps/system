"""Home (sd:2117): the design's Home Assistant tiles, from the config folder; no Home Assistant state is read yet."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import home_screen

    return home_screen.document(now=read.now)


PAGE = Page(
    section="Home",
    routes=("/home",),
    html="home.html",
    item="sd:2117",
    api=(Api("/api/home", _read),),
)
