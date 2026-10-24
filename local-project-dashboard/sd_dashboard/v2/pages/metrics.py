"""Metrics (sd:2119): spend against budget, the week, providers, the month's usage and age in status, from
the reads the classic Usage area and the retired Progress area make. Its Cap and Configure post the routes the classic
Usage area's forms post (sd:3387). That area stays in the palette: its month picker reads past months, and this page
reads the current month only."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import metrics_screen

    return metrics_screen.document(read.connection, now=read.now, kept=read.metrics)


PAGE = Page(
    section="Metrics",
    routes=("/metrics",),
    html="metrics.html",
    item="sd:2119",
    api=(Api("/api/metrics", _read),),
    count="/api/metrics",
    classic={"Usage (classic)": "/operations?area=usage"},
)
