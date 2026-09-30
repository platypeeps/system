"""The dashboard's default UI: the design source's shell, and the pages built on it so far (sd:2110, sd:2163, sd:2124, sd:2111).

The design source is ui-design `products/system/` (`design.md`, `designs/v2/`,
`foundation/`); `static/` holds its shell, tokens and fonts as the files the
dashboard policy allows (`default-src 'self'`: no inline style or script, no
font from another origin). `static/shell.js` marks each change from the
reference with `build:`.

A page is one HTML file beside this module; its data arrives by the JSON
routes v1 already serves, so a v2 page adds no second read of the store.
The assets are what `static/` holds, enumerated from the directory when the
module loads: a file added there is served, and nothing outside it can be.

Since sd:2163 the pages are served at the root (`/today`) and the assets under
`/ui/`, a prefix no v1 route uses; v1 keeps `/static/`. A `/v2/` address that
names a page or an asset redirects to its new one (`moved`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["ASSETS", "CLASSIC", "PAGES", "SCREENS", "SECTIONS", "asset", "moved", "page"]

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: Route path -> page file. `/` is Today as well: the new design is the default.
PAGES = {"/": "today.html", "/today": "today.html", "/tasks": "tasks.html", "/activity": "activity.html"}

#: The shell's map, and the only place a rail section names its address. A ported section opens its page; a section
#: not yet ported opens its old screen, marked "classic" on the rail, until its port moves it into SECTIONS. A section
#: in neither has no old screen and still says it is not built. The shell reads the three as /ui/sections.js.
SECTIONS = {"Today": "/today", "Tasks": "/tasks", "Activity": "/activity"}
CLASSIC = {
    "Writing": "/writing",
    "Research": "/operations?area=resources",
    "Contributions": "/contributions",
    "Documents": "/documents",
    "Skills": "/skills",
    "Metrics": "/operations?area=usage",
    "Management": "/operations?area=jobs",
    "Reports": "/operations?area=reports",
    "Commands": "/operations?area=commands",
    "Designs": "/designs",
}
#: Old screens with no rail section of their own; the palette offers them under "Classic screens". The old Backlog stays
#: here after Tasks moved to `SECTIONS` (sd:2124): the new page has no run selection, age buckets, paging or status
#: filter yet, and Operations > Progress links to it.
SCREENS = {
    "Today (classic)": "/classic/today",
    "Backlog (classic)": "/backlog",
    "Jobs": "/operations?area=jobs",
    "Services": "/operations?area=services",
    "Ports": "/operations?area=ports",
    "Trackers": "/operations?area=trackers",
    "Repos": "/operations?area=repos",
    "Sessions": "/operations?area=sessions",
    "Progress": "/operations?area=progress",
    "Protection": "/protection",
}

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
    lines = ["// The rail's section map, written by sd_dashboard/v2/__init__.py (sd:2163); edit it there."]
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
