"""HOA (sd:2116): the water system's map, Mission export trends, open followups and alarm events, from the checkout
the repo table holds as `hoa`. The page has no classic screen to keep: v1 had no HOA view. Acknowledge posts
/api/hoa/alarm (sd:4967), the view state Designs' Hide writes. Run sweep posts /api/hoa/sweep (sd:5095), which starts the
checkout's tools/sweep.py in the background; GET /api/hoa/sweep/status reads its last run. Download posts
/api/hoa/download (sd:5626), which opens the source's portal in a Claude Chrome session, waits for the operator's login,
then runs the checkout's download; /api/hoa's `downloads` part reads each source's last run."""

from __future__ import annotations

from . import Api, Page, place_views


def _read(read):
    from ... import hoa_screen

    return hoa_screen.document(read.connection, now=read.now, jobs=read.jobs)


def _sweep(read):
    from ... import hoa_screen, sweep

    return sweep.status(hoa_screen.checkout(read.connection)[1])


def _run_sweep(payload, principal):
    from ... import sweep

    return sweep.request(dict(payload))


def _download(payload, principal):
    from ... import hoa_download

    return hoa_download.request(dict(payload))


def _ack(payload, principal):
    from ... import hoa_screen

    return hoa_screen.acknowledge(payload)


PAGE = Page(
    section="HOA",
    routes=("/hoa",),
    html="hoa.html",
    item="sd:2116",
    # HOA's Tasks view is /api/hoa's items (hoa.js draws it), so it has no read of its own.
    api=(Api("/api/hoa", _read), Api("/api/hoa/alarm", write=_ack), Api("/api/hoa/sweep", write=_run_sweep),
         Api("/api/hoa/sweep/status", _sweep), Api("/api/hoa/download", write=_download),
         *place_views("hoa", ("briefs", "reports"))),
    count="/api/hoa",
)
