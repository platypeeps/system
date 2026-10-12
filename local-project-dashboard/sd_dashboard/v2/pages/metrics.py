"""Metrics (sd:2119): spend against budget, the week, providers, skill use, the month's usage and age in status, from
the reads the classic Usage and Progress areas make. Its Cap and Configure post the routes the classic Usage area's forms
post (sd:3387); that area stays in the palette until slice 10 of sd:1333 retires it."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import metrics_screen

    return metrics_screen.document(read.connection, now=read.now)


PAGE = Page(
    section="Metrics",
    routes=("/metrics",),
    html="metrics.html",
    item="sd:2119",
    api=(Api("/api/metrics", _read),),
    classic={"Usage (classic)": "/operations?area=usage"},
)
