"""The v2 dashboard: the ui-design shell, and the pages built on it so far (sd:2110).

The design source is ui-design `products/system/` (`design.md`, `designs/v2/`,
`foundation/`); `static/` holds its shell, tokens and fonts as the files the
dashboard policy allows (`default-src 'self'`: no inline style or script, no
font from another origin). `static/shell.js` marks each change from the
reference with `build:`.

A page is one HTML file beside this module; its data arrives by the JSON
routes v1 already serves, so a v2 page adds no second read of the store.
The assets are what `static/` holds, enumerated from the directory when the
module loads: a file added there is served, and nothing outside it can be.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ASSETS", "PAGES", "asset", "page"]

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: Route path -> page file. The v1 routes stay where they are.
PAGES = {"/v2/today": "today.html"}

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


#: Published name (relative to /v2/static/) -> content type.
ASSETS = _enumerate()


def page(path: str) -> str | None:
    """The page at a v2 route, or None when the path is not one."""
    name = PAGES.get(path)
    return None if name is None else (HERE / name).read_text(encoding="utf-8")


def asset(name: str) -> tuple[bytes, str] | None:
    """One file from `static/`, by its exact published name, or None."""
    kind = ASSETS.get(name)
    return None if kind is None else ((STATIC / name).read_bytes(), kind)
