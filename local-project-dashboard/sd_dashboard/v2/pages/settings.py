"""Settings (sd:3438): what this browser keeps for the dashboard, the theme (theme.js) and the sections width
(shell.railWidth, sd:3445). Both live in the browser's storage, so the page has no API route and writes nothing."""

from __future__ import annotations

from . import Page

PAGE = Page(
    section="Settings",
    routes=("/settings",),
    html="settings.html",
    item="sd:3438",
)
