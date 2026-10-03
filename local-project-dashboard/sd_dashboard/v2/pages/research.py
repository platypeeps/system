"""Research (sd:2122): the board, and one checkout's source registry, from collect_research in a child. The classic
Resources table stays in the palette: it also holds Toolbox, Briefs, Vault and Queues, which have no page yet."""

from __future__ import annotations

from . import Api, Page


def _board(read):
    from ... import research_screen

    return research_screen.document(now=read.now)


def _sources(read):
    from ... import research_screen

    found = research_screen.sources(read.path[len("/api/research/"):], now=read.now)
    if found is None:
        return 404, {"error": "No research checkout at this address."}
    return found


PAGE = Page(
    section="Research",
    routes=("/research",),
    html="research.html",
    item="sd:2122",
    api=(Api("/api/research", _board), Api(pattern=r"/api/research/.*", example="/api/research/example", read=_sources)),
    classic={"Resources (classic)": "/operations?area=resources"},
)
