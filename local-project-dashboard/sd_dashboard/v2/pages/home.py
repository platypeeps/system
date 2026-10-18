"""Home (sd:2117): the design's Home Assistant tiles, from the setting row or the config folder (sd:3987), with live
state (sd:3427). Edit tiles posts /api/home/tiles; a tile's Act section posts /api/home/service (sd:4966)."""

from __future__ import annotations

from . import Api, Page, place_views


def _read(read):
    from ... import home_screen

    return home_screen.document(read.connection, now=read.now)


def _save(payload, principal):
    from ... import home_screen

    return home_screen.save(payload)


def _service(payload, principal):
    from ... import home_screen

    return home_screen.service(payload, principal)


PAGE = Page(
    section="Home",
    routes=("/home",),
    html="home.html",
    item="sd:2117",
    api=(Api("/api/home", _read), Api("/api/home/tiles", write=_save), Api("/api/home/service", write=_service),
         *place_views("home")),
)
