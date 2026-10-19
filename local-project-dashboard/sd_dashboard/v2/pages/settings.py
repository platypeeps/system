"""Settings (sd:3438): what this browser keeps for the dashboard, the theme (theme.js) and the sections width
(shell.railWidth, sd:3445), in the browser's storage; and above them the Fleet section (sd:3989), the `sd.*` keys one value
serves on every machine, read and set through `sd config` (`settings_screen`); and Sweeps (sd:5109), the Work and Personal
sweeps' setting rows, read and set on the page's connection."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import settings_screen

    return {**settings_screen.document(now=read.now), **settings_screen.sweeps(read.connection)}


def _set(payload, principal):
    from ... import settings_screen

    return settings_screen.request(payload)


def _set_sweep(payload, principal):
    from ... import settings_screen

    return settings_screen.request_sweep(dict(payload))


PAGE = Page(
    section="Settings",
    routes=("/settings",),
    html="settings.html",
    item="sd:3438",
    api=(Api("/api/settings", _read), Api("/api/settings/fleet", write=_set),
         Api("/api/settings/sweep", write=_set_sweep)),
)
