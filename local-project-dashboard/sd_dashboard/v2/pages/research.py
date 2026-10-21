"""Research (sd:2122): the board, and one checkout's source registry, from collect_research in a child. The classic
Resources table stays in the palette: it also holds Toolbox, Vault and Queues, which have no page yet. A project's
experiment in the work Notion (sd:5296) is `/api/research/experiments`: GET reads the link, the list and the runs; POST
`/api/research/experiments/act` attaches, drafts and applies (`research_experiments`). Build deck (pack sd:5525) posts
`/api/research/deck` and reads `/api/research/deck/status` (the last run) and `/api/research/deck/<id>` (`research_deck`);
the built deck downloads from `/research/deck/<key>` (`server`). Import to Slides posts `/api/research/slides` and reads
`/api/research/slides/status` (`research_slides`). Each is listed before the checkout pattern, which would
match it."""

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


def _deck(payload, principal):
    from ... import research_deck

    return research_deck.request(dict(payload))


def _deck_last(read):
    from ... import research_deck

    return research_deck.latest()


def _deck_run(read):
    from ... import research_deck

    return research_deck.status(read.path.rsplit("/", 1)[1])


def _slides(payload, principal):
    from ... import research_slides

    return research_slides.request(dict(payload))


def _slides_status(read):
    from ... import research_slides

    return research_slides.status()


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
         Api("/api/research/experiments/act", write=_experiment), Api("/api/research/deck", write=_deck),
         Api("/api/research/deck/status", _deck_last), Api("/api/research/slides", write=_slides),
         Api("/api/research/slides/status", _slides_status),
         Api(pattern=r"/api/research/deck/deck-[0-9TZ]+", example="/api/research/deck/deck-20261020T120000Z", read=_deck_run),
         Api(pattern=r"/api/research/.*", example="/api/research/example", read=_sources)),
    classic={"Resources (classic)": "/operations?area=resources"},
    count="/api/research",
)
