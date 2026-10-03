"""Today (sd:2110, the default since sd:2163): the ranked rows of /api/now, which stays in server.py's chain because the
classic Now panel reads it too. The old Today moved to /classic/today when this page took `/`."""

from __future__ import annotations

from . import Old, Page


def _classic(read):
    from ... import screens

    return screens.today(read.connection, now=read.now, parameters=read.parameters)


PAGE = Page(
    section="Today",
    routes=("/today", "/"),
    html="today.html",
    item="sd:2110",
    classic={"Today (classic)": "/classic/today"},
    takes=(Old("/classic/today", _classic),),
)
