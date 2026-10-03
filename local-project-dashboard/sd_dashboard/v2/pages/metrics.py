"""Metrics (sd:2119): spend against budget, the week, providers, skill use, the month's usage and age in status, from
the reads the classic Usage and Progress areas make. The classic Usage area stays in the palette: it carries the bill-cap
and provider-control forms, which this page does not."""

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
