"""Settings (sd:3438): what this browser keeps for the dashboard, the theme (theme.js) and the sections width
(shell.railWidth, sd:3445), in the browser's storage; and above them the Fleet section (sd:3989), the `sd.*` keys one value
serves on every machine, read and set through `sd config` (`settings_screen`)."""

from __future__ import annotations

from . import Api, Page


def _read(read):
    from ... import settings_screen

    return settings_screen.document(now=read.now)


def _set(payload, principal):
    from ... import settings_screen

    return settings_screen.request(payload)


PAGE = Page(
    section="Settings",
    routes=("/settings",),
    html="settings.html",
    item="sd:3438",
    api=(Api("/api/settings", _read), Api("/api/settings/fleet", write=_set)),
)
