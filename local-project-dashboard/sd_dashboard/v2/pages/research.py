"""Research (sd:2122): the board, and one checkout's source registry, from collect_research in a child. The classic
Resources table stays in the palette: it also holds Toolbox, Vault and Queues, which have no page yet. A project's
experiment in the work Notion (sd:5296) is `/api/research/experiments`: GET reads the link, the list and the runs; POST
`/api/research/experiments/act` attaches, drafts and applies (`research_experiments`). It is listed before the checkout pattern, which would match it."""

from __future__ import annotations

from . import Api, Page


def _board(read):
    from ... import research_screen

    return research_screen.document(now=read.now)


def _experiments(read):
    from ... import research_experiments

    return research_experiments.document(read.connection, (read.parameters.get("project") or [""])[0])


def _experiment(payload, principal):
    from ... import research_experiments

    return research_experiments.request(dict(payload))


def _sources(read):
    from ... import research_screen

    found = research_screen.sources(read.path[len("/api/research/"):], now=read.now, connection=read.connection)
    if found is None:
        return 404, {"error": "No research checkout at this address."}
    return found


PAGE = Page(
    section="Research",
    routes=("/research",),
    html="research.html",
    item="sd:2122",
    api=(Api("/api/research", _board), Api("/api/research/experiments", _experiments, query=True),
         Api("/api/research/experiments/act", write=_experiment), Api(pattern=r"/api/research/.*", example="/api/research/example", read=_sources)),
    classic={"Resources (classic)": "/operations?area=resources"},
    count="/api/research",
)
