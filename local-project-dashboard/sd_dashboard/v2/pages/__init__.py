"""The v2 pages, one module each (sd:2418).

Each module beside this one defines `PAGE`, a `Page`: the rail section it
fills, its routes and HTML file, its API routes, and any old screen it keeps in
the palette or takes the path of. The modules are enumerated when this package
loads, as `v2` enumerates `static/`, so a port adds a module here and edits no
shared line: `v2.PAGES`, `v2.SECTIONS`, `/ui/sections.js`, the server's API
dispatch and the old-screen routes are all built from `PAGES`.

A module imports its screen on first use, inside the function that reads it,
as `server.py` did: enumerating the pages loads no collector.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

__all__ = ["Api", "Old", "PAGES", "Page", "Read", "collect"]


@dataclass(frozen=True)
class Read:
    """What one request hands a page's read, write or old-screen render."""

    connection: Any = None
    now: str = ""
    path: str = ""
    parameters: dict = field(default_factory=dict)
    fleet: Any = None
    jobs: Any = None
    services: Any = None
    ports: Any = None


@dataclass(frozen=True)
class Api:
    """One API route. `read(read)` answers a GET with a document (a 200) or `(status, document)`.

    `write(payload, principal)` is a POST, with `action_route`'s contract: it returns a callable that takes a write
    connection, or raises `ValueError`. Only a write the page alone posts registers here; one v1 also posts stays in
    `action_route`. `pattern` is a full-match regex for a path with an id, and `example` a path it matches, which the
    registry test requests. A route refuses a query string unless `query` is true.
    """

    path: str = ""
    read: Callable | None = None
    write: Callable | None = None
    pattern: str = ""
    example: str = ""
    query: bool = False

    def matches(self, path: str) -> bool:
        return path == self.path if self.path else re.fullmatch(self.pattern, path) is not None


@dataclass(frozen=True)
class Old:
    """An old screen whose path the new page took, served at `path` by `render(read)`."""

    path: str
    render: Callable[[Read], str]


@dataclass(frozen=True)
class Page:
    """One v2 page. `routes[0]` is the address the rail opens; `classic` maps a palette label to an old screen."""

    section: str
    routes: tuple[str, ...]
    html: str
    item: str
    api: tuple[Api, ...] = ()
    classic: dict[str, str] = field(default_factory=dict)
    takes: tuple[Old, ...] = ()
    name: str = ""


def collect(paths, package: str) -> tuple[Page, ...]:
    """Every module's `PAGE` under `paths`, in module-name order; a module without one fails the import."""
    found = []
    for module in sorted(name for _, name, _ in pkgutil.iter_modules(paths)):
        page = getattr(importlib.import_module(f"{package}.{module}"), "PAGE", None)
        if not isinstance(page, Page):
            raise ImportError(f"{package}.{module} defines no PAGE: every module in the page registry is one page")
        found.append(replace(page, name=module))
    return tuple(found)


#: Every registered page.
PAGES = collect(__path__, __name__)
