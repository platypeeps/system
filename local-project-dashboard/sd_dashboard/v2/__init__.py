"""The dashboard's default UI: the design source's shell, and the pages built on it so far (sd:2163).

The design source is ui-design `products/system/` (`design.md`, `designs/pages/`
and `foundation/`); `static/` holds its shell, tokens and fonts as the files the
dashboard policy allows (`default-src 'self'`: no inline style or script, no
font from another origin). `static/shell.js` marks each change from the
reference with `build:`.

A page is one HTML file beside this module, registered by its own module in
`pages/` (sd:2418): `PAGES`, `SECTIONS`, the classic screens it keeps, its API
routes (`api`, `action`) and the old screens it took the path of (`old`) are
built from that enumeration, so a port edits no line here. `dashboard.sh pages`
prints the map. The assets are what `static/` holds, enumerated from the
directory when the module loads: a file added there is served, and nothing
outside it can be.

Since sd:2163 the pages are served at the root (`/today`) and the assets under
`/ui/`, a prefix no v1 route uses; v1 keeps `/static/`. A `/v2/` address that
names a page or an asset redirects to its new one (`moved`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import pages as registry

__all__ = ["ASSETS", "CLASSIC", "PAGES", "SCREENS", "SECTIONS", "action", "api", "asset", "listing", "moved", "old", "page"]

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: Route path -> page file, from every registered page's routes. Today's include `/`: the new design is the default.
PAGES = {route: entry.html for entry in registry.PAGES for route in entry.routes}

#: The shell's map: a registered section opens its page's first route. A section not yet ported opens its old screen,
#: marked "classic" on the rail (CLASSIC), until its page registers. A section in neither has no old screen and still
#: says it is not built. The shell reads the three as /ui/sections.js.
SECTIONS = {entry.section: entry.routes[0] for entry in registry.PAGES}
CLASSIC = {section: href for section, href in {
    "Writing": "/writing",
    "Skills": "/skills",
    "Metrics": "/operations?area=usage",
    "Commands": "/operations?area=commands",
    "Designs": "/designs",
}.items() if section not in SECTIONS}
#: Old screens with no rail section of their own; the palette offers them under "Classic screens". These belong to no
#: page; Jobs, Services, Repos, Sessions and Protection stayed when Management moved (sd:2118). An old screen a page
#: keeps (its `classic`, with the reason in its module) follows, by label.
SCREENS = {
    "Jobs": "/operations?area=jobs",
    "Services": "/operations?area=services",
    "Ports": "/operations?area=ports",
    "Trackers": "/operations?area=trackers",
    "Repos": "/operations?area=repos",
    "Sessions": "/operations?area=sessions",
    "Progress": "/operations?area=progress",
    "Protection": "/protection",
} | dict(sorted((label, href) for entry in registry.PAGES for label, href in entry.classic.items()))

TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _enumerate() -> dict[str, str]:
    found = {}
    for directory, _, files in os.walk(STATIC):
        for name in files:
            path = Path(directory) / name
            kind = TYPES.get(path.suffix)
            if kind:
                found[path.relative_to(STATIC).as_posix()] = kind
    return found


def _sections() -> bytes:
    lines = ["// The rail's section map, written by sd_dashboard/v2/__init__.py from the page registry in sd_dashboard/v2/pages/ (sd:2418)."]
    for key, value in (("SHELL_PAGES", SECTIONS), ("SHELL_CLASSIC", CLASSIC), ("SHELL_SCREENS", SCREENS)):
        lines.append(f"window.{key} = {json.dumps(value)};")
    return ("\n".join(lines) + "\n").encode("utf-8")


#: Generated asset name -> its bytes; served beside the files in `static/`.
GENERATED = {"sections.js": _sections()}

#: Published name (relative to /ui/) -> content type.
ASSETS = _enumerate() | {name: TYPES[Path(name).suffix] for name in GENERATED}


def page(path: str) -> str | None:
    """The page at a route, or None when the path is not one."""
    name = PAGES.get(path)
    return None if name is None else (HERE / name).read_text(encoding="utf-8")


def api(path: str):
    """The registered page and API route that answer a GET at `path`, or None."""
    for entry in registry.PAGES:
        for route in entry.api:
            if route.read and route.matches(path):
                return entry, route
    return None


def action(path: str):
    """The registered write that answers a POST at `path`, or None; a write v1 posts too stays in `action_route`."""
    for entry in registry.PAGES:
        for route in entry.api:
            if route.write and route.matches(path):
                return route
    return None


def old(path: str):
    """The old screen a registered page took the path of, served at `path`, or None."""
    for entry in registry.PAGES:
        for screen in entry.takes:
            if screen.path == path:
                return screen
    return None


def listing() -> list[str]:
    """The rail and palette map, one tab-separated line per target, as `dashboard.sh pages` prints it.

    A registered page is `section, address, new, item`; an unported section is `section, address, classic`; an old
    screen the palette offers is `label, address, palette`.
    """
    return ([f"{entry.section}\t{entry.routes[0]}\tnew\t{entry.item}" for entry in registry.PAGES]
            + [f"{section}\t{href}\tclassic" for section, href in CLASSIC.items()]
            + [f"{label}\t{href}\tpalette" for label, href in SCREENS.items()])


def asset(name: str) -> tuple[bytes, str] | None:
    """One file from `static/` or a generated one, by its exact published name, or None."""
    kind = ASSETS.get(name)
    if kind is None:
        return None
    return (GENERATED[name] if name in GENERATED else (STATIC / name).read_bytes()), kind


def moved(path: str) -> str | None:
    """The new address of a `/v2/` page or asset, or None when the path names neither.

    Only a page or asset that exists redirects, so a crafted path such as
    `/v2//example.test` can never become a Location to another host.
    """
    if path.startswith("/v2/static/"):
        name = path[len("/v2/static/") :]
        return f"/ui/{name}" if name in ASSETS else None
    if path.startswith("/v2/"):
        target = "/" + path[len("/v2/") :]
        return target if target != "/" and target in PAGES else None
    return None
