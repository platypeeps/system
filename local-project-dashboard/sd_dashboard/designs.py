"""Design mockups from the ui-design checkout, listed here and served as drawn.

Documents serves finished, self-contained reports one directory deep. A design
mockup is neither: it links a shared stylesheet three directories up
(`../../../foundation/tokens.css`), shows screenshots beside it, and runs a
script so its drawer and palette can be tried on the iPad it was drawn for.
So this tab serves the checkout's tree under `/designs/`, at the same relative
paths, and a mockup's relative links land where its author meant.

Three rules keep that from serving more than designs.

**Every segment is a name.** Each path segment must start with a letter or a
digit, so `..`, `.git` and every dotfile are unsayable, and the last one must
carry an extension from `TYPES`. A request is decoded once before the check, so
`%2e%2e` is the `..` it spells.

**The resolved file stays in the checkout.** The path is resolved strictly and
must still sit under the resolved root; a symlink that points out is a 404.

**A mockup runs sandboxed.** Its page carries `sandbox allow-scripts`, which
gives it an opaque origin: its script runs, but it reads no dashboard cookie and
calls no dashboard endpoint as the operator. `connect-src` and `form-action` are
closed as well. Because that origin is opaque, the mockup's own stylesheet and
images reach it as cross-origin loads, so asset responses carry
`Cross-Origin-Resource-Policy: cross-origin` instead of the dashboard's
`same-origin`. They are design assets from one checkout, behind the same host
check as every other page.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

from .markup import join, tag
from .pages import empty, page

#: The checkout the tab serves. `DESIGNS_ROOT` moves it; the default follows
#: `REPO_ROOT` the way the collectors and the documents scan do.
ROOT = Path(os.environ.get(
    "DESIGNS_ROOT",
    str(Path(os.environ.get("REPO_ROOT", "~/repos")) / "platypeeps" / "ui-design"),
)).expanduser()

#: Where the checkout keeps one folder per product.
PRODUCTS = "products"

#: The extensions served, each with the type it is served as. Anything else is
#: a 404, which keeps Markdown briefs and source files off the wire.
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: The mockup page's policy, replacing the dashboard's for that one response.
POLICY = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob:; "
    "connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)

#: Headers an asset response carries in place of the dashboard's.
ASSET_HEADERS = {"Cross-Origin-Resource-Policy": "cross-origin"}


def resolve(tail: str, root: Path | None = None) -> tuple[Path, str] | None:
    """The file and content type this address names, or None.

    The listing and the server both call it, so a link the listing offers is
    a link the server answers. None for every way of missing, so a 404 says
    nothing about which check refused.
    """
    segments = unquote(tail or "").split("/")
    if not all(SEGMENT.fullmatch(segment) for segment in segments):
        return None
    kind = TYPES.get(Path(segments[-1]).suffix.lower())
    if kind is None:
        return None
    try:
        base = Path(ROOT if root is None else root).expanduser().resolve(strict=True)
        target = base.joinpath(*segments).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target, kind


def designs(root: Path | None = None) -> dict[str, list[str]]:
    """Every servable HTML page under `products/`, by product, as relative paths."""
    base = Path(ROOT if root is None else root).expanduser()
    found: dict[str, list[str]] = {}
    products = base / PRODUCTS
    if not products.is_dir():
        return found
    for product in sorted(p for p in products.iterdir() if p.is_dir()):
        pages = []
        for current, folders, files in os.walk(product):
            folders[:] = sorted(f for f in folders if SEGMENT.fullmatch(f))
            for name in sorted(files):
                relative = (Path(current) / name).relative_to(base).as_posix()
                if name.endswith(".html") and resolve(relative, base) is not None:
                    pages.append(relative)
        if pages:
            found[product.name] = pages
    return found


def render(parameters=None, *, root: Path | None = None) -> str:
    """The listing: one section per product, each page linked at its tree path."""
    del parameters  # No facets; every product is always shown.
    base = Path(ROOT if root is None else root).expanduser()
    found = designs(base)
    if not found:
        return page("Designs", "designs",
                    empty("No design pages found in %s." % (base / PRODUCTS),
                          "add an HTML page under %s/<product>/designs/" % PRODUCTS))
    sections = []
    for product, pages in found.items():
        prefix = f"{PRODUCTS}/{product}/"
        sections.append(tag(
            "section",
            tag("h2", product),
            tag("ul", join(
                tag("li", tag("a", path[len(prefix):], href=f"/designs/{path}"))
                for path in pages
            ), class_="documents"),
        ))
    return page(
        "Designs", "designs",
        tag("p", "Mockups from %s, served as drawn. Each page runs sandboxed: its script "
                 "works, but it cannot act on this dashboard." % base, class_="hint"),
        join(sections),
    )
