"""Static files cached under their content hash, and sent gzipped when the browser takes it (sd:2141).

A page links each asset as `<name>?v=<digest>`, from a map computed once when
the server starts; there is no build step and no hashed copy on disk. The
server answers `Cache-Control: IMMUTABLE` only when `v` is the digest of the
bytes it is sending at that moment, so a cached copy never outlives its bytes:
a file edited after start keeps the old `v` in the pages until a restart, and
a request for that `v` gets the new bytes with `no-store`. A request without
`v`, or with a stale one, is `no-store` as before.

Only static text is compressed. Pages and API answers stay as they are: they
carry the session's CSRF token next to text a request can choose, which is the
shape a compression side channel needs.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path

#: A year, private to the browser: the dashboard sits behind its own sign-in.
IMMUTABLE = "private, max-age=31536000, immutable"

#: v1's folder and its two files, which `pages.page` links and `/static/` serves.
STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = ("dashboard.css", "dashboard.js")


def digest(body: bytes) -> str:
    """The `v` for these bytes: 16 hex digits of their SHA-256."""
    return hashlib.sha256(body).hexdigest()[:16]


def cache_control(body: bytes, query: dict) -> str:
    """IMMUTABLE when the request's one `v` names these bytes, else `no-store`."""
    named = query.get("v", [])
    return IMMUTABLE if len(named) == 1 and named[0] == digest(body) else "no-store"


def link(text: str, prefix: str, versions: dict[str, str]) -> str:
    """`src="<prefix><name>"` and `href=` rewritten to carry `?v=` for each name in `versions`.

    A name the map does not hold, or a link that already has a query, is left as
    it is, so the rewrite can only add a cache key and never point elsewhere.
    """
    def versioned(match: re.Match) -> str:
        name = match.group(3)
        version = versions.get(name)
        if version is None:
            return match.group(0)
        return f'{match.group(1)}="{match.group(2)}{name}?v={version}"'

    return re.sub(rf'\b(src|href)="({re.escape(prefix)})([^"?#]+)"', versioned, text)


def compressible(content_type: str) -> bool:
    """Text the browser can take gzipped: CSS, script and plain text, not fonts."""
    kind = content_type.split(";", 1)[0].strip().lower()
    return kind.startswith("text/") or kind in {"application/javascript", "application/json"}


def accepts_gzip(header: str | None) -> bool:
    """Whether `Accept-Encoding` takes gzip: named (or `*`) without `q=0`."""
    offered = {}
    for part in (header or "").split(","):
        token, _, parameters = part.partition(";")
        token = token.strip().lower()
        if not token:
            continue
        weight = 1.0
        match = re.search(r"\bq\s*=\s*([0-9.]+)", parameters)
        if match:
            try:
                weight = float(match.group(1))
            except ValueError:
                weight = 0.0
        offered[token] = weight
    weight = offered.get("gzip", offered.get("*", 0.0))
    return weight > 0


def gzipped(body: bytes) -> bytes:
    """Gzip with no timestamp, so the same bytes always compress to the same answer."""
    return gzip.compress(body, mtime=0)


#: v1's two files under their digests, read when the module loads.
STATIC_VERSIONS = {name: digest((STATIC / name).read_bytes()) for name in STATIC_FILES}
