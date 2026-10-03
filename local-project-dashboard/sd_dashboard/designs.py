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
closed as well, so a mockup that needs data ships it as a script, not a fetch.
Shared scripts (`foundation/theme.js`, a product's shell) are served like its
stylesheet: they run inside that sandbox, with the page's policy. Because that origin is opaque, the mockup's own stylesheet and
images reach it as cross-origin loads, so asset responses carry
`Cross-Origin-Resource-Policy: cross-origin` instead of the dashboard's
`same-origin`. They are design assets from one checkout, behind the same host
check as every other page.

**The ledger says what each page is and whether its screenshot is current.**
`ledger()` is what ui-design's `tools/collect-designs.mjs` writes as
`window.DESIGNS`, read live: each page's kind, title, size and last commit, its
screenshots and which of them are stale, and each product's brief, including a
product with a brief and no page yet. A screenshot is stale when the inputs hash
`shots.mjs` recorded for it differs from the page's hash now; `inputs_hash()`
ports `tools/inputs.mjs` byte for byte, because the two sides compare the same
recorded values. Every page and shot it lists passes `resolve()`: the ledger
names nothing the tab would answer with a 404.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
from datetime import datetime, timezone
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
    ".js": "text/javascript; charset=utf-8",
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

#: A font's headers. The sandboxed page has an opaque origin, and a browser
#: fetches a font in CORS mode, so without this header every mockup falls back
#: to system fonts. Only fonts get it: the data scripts beside the pages hold
#: real notes and mail, and any site could read them from the loopback port.
FONT_HEADERS = {**ASSET_HEADERS, "Access-Control-Allow-Origin": "*"}


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


# ---------------------------------------------------------------------------
# The inputs hash: a port of ui-design's tools/inputs.mjs. Each rule below
# mirrors one line there; a change on either side stales every screenshot.

#: The mockup folder, `designs/pages/` since the design source's sd:2193; `designs/v2/` is no longer read (sd:2454).
_MOCKUP = "pages"
#: Generated data files the hash treats specially, by where they sit.
SKIP = re.compile(rf"/{_MOCKUP}/data/counts\.js$")
STAMP = re.compile(rf"/{_MOCKUP}/data/commands\.js$")
LEDGER = re.compile(rf"/{_MOCKUP}/data/designs-data\.js$")
#: Ledger fields a collect or a retake rewrites, left out of the hash.
SHOT_FIELDS = ("dirty", "shotStale", "shotTimes", "shotChanged", "shotSha", "shotOldest", "shotSize")
#: Where shots.mjs records each screenshot's inputs hash.
MANIFEST = "products/system/designs/shots/inputs.json"

_LOADS = re.compile(r'<(?:script|link|img|source|iframe)\b[^>]*?\s(?:src|href)="([^"]+)"')
_CSS_REFS = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)|@import\s+['"]([^'"]+)['"]""")
_NOT_LOCAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#|data:)", re.IGNORECASE)


def _text(data: bytes) -> str:
    """Node's `String(buffer)`: UTF-8, each bad sequence one U+FFFD."""
    return data.decode("utf-8", errors="replace")


def _join(*parts: str) -> str:
    """Node's `path.join`: every part appended, even one starting with `/`."""
    return posixpath.normpath("/".join(part for part in parts if part))


def _relative(root: str, path: str) -> str:
    """Node's `path.relative`, which says `""` for the root itself."""
    found = posixpath.relpath(path, root)
    return "" if found == "." else found


def _local(url: str) -> bool:
    return not _NOT_LOCAL.match(url) and "${" not in url


def refs(root: Path | str, page: str) -> list[str]:
    """What a page draws from, as repository paths, the page first.

    The page, every local file a script, link, img, source or iframe tag names,
    and every local file those stylesheets reach through url() or @import. A
    link to another page is not an input: its change does not change this look.
    """
    root = str(root)
    html = _text(Path(root, page).read_bytes())
    out, seen = [page], {page}

    def add(source: str, url: str) -> str | None:
        found = _relative(root, _join(root, posixpath.dirname(source), re.split(r"[?#]", url)[0]))
        if found in seen:
            return None
        seen.add(found)
        out.append(found)
        return found

    def walk_css(css: str) -> None:
        if not os.path.isfile(os.path.join(root, css)):
            return
        for match in _CSS_REFS.finditer(_text(Path(root, css).read_bytes())):
            url = (match.group(1) or match.group(2)).strip()
            if not _local(url):
                continue
            found = add(css, url)
            if found and found.endswith(".css"):
                walk_css(found)

    for match in _LOADS.finditer(html):
        if not _local(match.group(1)):
            continue
        found = add(page, match.group(1))
        if found and found.endswith(".css"):
            walk_css(found)
    return out


def _js_number(text: str) -> float | int:
    """A JSON number as `JSON.stringify` writes it back: `2.0` is `2`."""
    value = float(text)
    return int(value) if value.is_integer() and abs(value) < 2 ** 53 else value


def _ledger_text(text: str) -> str:
    """The Designs ledger as the hash sees it: no stamps, no per-shot fields."""
    start = text.index("=", text.index("window.DESIGNS")) + 1
    data = json.loads(re.sub(r";\s*$", "", text[start:].strip()), parse_float=_js_number)
    for key in ("read", "head", "branch"):
        data.pop(key, None)
    for entry in data.get("pages") or []:
        for key in SHOT_FIELDS:
            entry.pop(key, None)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def inputs_hash(root: Path | str, page: str) -> str:
    """The page's inputs hash, as `inputsHash(root, page)` computes it."""
    root = str(root)
    digest = hashlib.sha256()
    for name in sorted(f for f in refs(root, page) if not SKIP.search(f)):
        target = os.path.join(root, name)
        body = _text(Path(target).read_bytes()) if os.path.isfile(target) else ""
        if STAMP.search(name):
            body = "\n".join(line for line in body.split("\n")
                             if not line.startswith("//") and not re.match(r'^\s*"read":', line))
        if LEDGER.search(name) and body != "":
            body = _ledger_text(body)
        digest.update((name + "\0" + body + "\0").encode("utf-8"))
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# The ledger: a port of ui-design's tools/collect-designs.mjs, read live.


def _git(base: Path, *arguments: str) -> str:
    """One git answer about the checkout, or "" when there is none."""
    try:
        done = subprocess.run(["git", "-C", str(base), "-c", "core.quotePath=false", *arguments],
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else ""


def _iso(value: str | float) -> str:
    """A commit time or an mtime as `toISOString()` gives it, less milliseconds."""
    moment = (datetime.fromtimestamp(value, timezone.utc) if isinstance(value, float)
              else datetime.fromisoformat(value).astimezone(timezone.utc))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _commits(base: Path) -> dict[str, tuple[str, str, str]]:
    """The last commit touching each path under `products/`: (sha, ISO time, subject).

    One history walk in place of a `git log -1` per path: the first commit that
    names a path is the newest. A merge names no path here, so a change made in
    a merge commit itself is dated by the commit before it.
    """
    found: dict[str, tuple[str, str, str]] = {}
    commit = None
    for line in _git(base, "log", "--format=%x1e%h%x09%cI%x09%s", "--name-only", "--", PRODUCTS).split("\n"):
        if line.startswith("\x1e"):
            sha, _, rest = line[1:].partition("\t")
            when, _, subject = rest.partition("\t")
            commit = (sha, _iso(when), subject)
        elif line and commit is not None:
            found.setdefault(line, commit)
    return found


def _dirty(base: Path) -> set[str]:
    """Paths the working tree changed or added under `products/`."""
    entries = _git(base, "status", "--porcelain", "-z", "--untracked-files=all", "--", PRODUCTS)
    return {entry[3:] for entry in entries.split("\0") if len(entry) > 3}


def _mockup(path: str) -> bool:
    """Whether the page sits in a product's mockup folder, under either name."""
    return re.search(rf"/designs/{_MOCKUP}/", path) is not None


def _kind(path: str) -> str:
    """Where the page sits says what it is."""
    if path.endswith("/template.html"):
        return "skeleton"
    if "/final/" in path:
        return "final"
    if "/reference/" in path:
        return "reference"
    if _mockup(path):
        return "v2 mockup"
    if "/designs/v1-" in path:
        return "v1 mockup"
    return "design"


def _shots(base: Path, path: str) -> list[str]:
    """A page's screenshots, by the names shots.mjs writes, as the tab serves them.

    A v2 page's are exactly `v2-<page>-<width>.png`, so a one-off capture beside
    them is not its shot; a v1 page's are every `v1-*.png`. A v2 page sits in
    `designs/pages/`; its shots sit in `designs/shots/`.
    """
    folder = posixpath.join(re.sub(rf"/{_MOCKUP}$", "", posixpath.dirname(path)), "shots")
    name = posixpath.basename(path)[:-len(".html")]
    if _mockup(path):
        prefix = f"v2-{name}-"

        def own(file: str) -> bool:
            return file.startswith(prefix) and re.fullmatch(r"\d+\.png", file[len(prefix):]) is not None
    elif name.startswith("v1-"):
        def own(file: str) -> bool:
            return file.startswith("v1-") and file.endswith(".png")
    else:
        return []
    try:
        files = os.listdir(base / folder)
    except OSError:
        return []
    shots = (f"{folder}/{file}" for file in sorted(files) if own(file))
    return [shot for shot in shots if resolve(shot, base) is not None]


def _png_size(target: Path) -> list[int] | None:
    """Width and height from a PNG's IHDR chunk, or None for a file too short to hold one."""
    try:
        with open(target, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24:
        return None
    return [int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")]


def _label(base: Path) -> str:
    """The main worktree, as `~/...`: a linked worktree's transient path is not the checkout."""
    common = _git(base, "rev-parse", "--git-common-dir").strip()
    main = (base / common).resolve().parent if common else base
    try:
        return "~/" + main.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(main)


def _brief(base: Path, name: str) -> tuple[str, str, str]:
    """(brief path, title, Status line) from a product's README.md; an empty brief is none.

    The brief is read only when it resolves inside the checkout, like a page.
    """
    brief = f"{PRODUCTS}/{name}/README.md"
    text = ""
    try:
        checkout = base.resolve(strict=True)
        target = (base / brief).resolve(strict=True)
        if target.is_relative_to(checkout) and target.is_file():
            text = _text(target.read_bytes())
    except (OSError, RuntimeError):
        pass
    status = re.search(r"^\*\*Status:\*\*\s*(.+)$", text, re.MULTILINE)
    title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return (brief if text else "", title.group(1) if title else name, status.group(1) if status else "")


def caution(page: dict) -> str:
    """Why a page needs a commit or a retake, or "" when it needs neither."""
    if page["dirty"]:
        return "uncommitted change in the working tree"
    if not page["shots"]:
        return "no screenshot"
    if page["shotStale"]:
        return "stale screenshot: " + ", ".join(posixpath.basename(f) for f in page["shotStale"])
    return ""


def ledger(root: Path | None = None, *, now: datetime | None = None) -> dict:
    """What the v2 Designs page reads as `window.DESIGNS`, from the checkout and its history.

    Adds `caution`, the badge count: pages uncommitted, without a screenshot,
    or with a stale one. A product with a brief and no page is listed with
    `pages: 0`; a product folder that resolves out of the checkout is not.
    """
    base = Path(ROOT if root is None else root).expanduser()
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out = {
        "read": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": _label(base) if base.is_dir() else str(base),
        "head": _git(base, "rev-parse", "--short", "HEAD").strip(),
        "branch": _git(base, "branch", "--show-current").strip(),
        "products": [], "pages": [], "caution": 0,
    }
    try:
        checkout = base.resolve(strict=True)
        names = sorted(p.name for p in (base / PRODUCTS).iterdir()
                       if SEGMENT.fullmatch(p.name) and p.is_dir()
                       and p.resolve().is_relative_to(checkout))
    except (OSError, RuntimeError):
        return out
    listed = designs(base)
    commits, dirty = _commits(base), _dirty(base)
    try:
        recorded = json.loads((base / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recorded = {}
    for name in names:
        brief, title, status = _brief(base, name)
        pages = listed.get(name, [])
        out["products"].append({"name": name, "title": title, "brief": brief,
                                "status": status, "pages": len(pages)})
        for path in pages:
            target = resolve(path, base)[0]
            html = _text(target.read_bytes())
            commit = commits.get(path)
            shots = _shots(base, path)
            stale: list[str] = []
            if _mockup(path) and shots:
                try:
                    current = inputs_hash(base, path)
                except (OSError, ValueError):
                    current = None  # An unreadable input: no recorded hash can match it.
                stale = [f for f in shots if recorded.get(posixpath.basename(f)) != current]
            times = {f: commits[f][1] if f in commits else "" for f in shots}
            # The page's shot age is its oldest capture's, so one fresh capture cannot hide a stale one.
            dated = sorted((times[f], f) for f in shots if times[f])
            oldest = dated[0][1] if dated else ""
            heading = re.search(r"<title>([^<]*)</title>", html)
            entry = {
                "path": path, "product": name, "kind": _kind(path), "bytes": target.stat().st_size,
                "title": heading.group(1).strip() if heading else "",
                "changed": commit[1] if commit else _iso(target.stat().st_mtime),
                "sha": commit[0] if commit else "", "subject": commit[2] if commit else "",
                "dirty": path in dirty, "shots": shots, "shotStale": stale,
                "shotSize": {f: _png_size(base / f) for f in shots}, "shotTimes": times,
                "shotChanged": times[oldest] if oldest else "",
                "shotSha": commits[oldest][0] if oldest else "", "shotOldest": oldest,
            }
            out["pages"].append(entry)
            if caution(entry):
                out["caution"] += 1
    return out


#: The generated ledger the v2 Designs page loads. The tab answers it live.
LIVE_LEDGER = f"products/system/designs/{_MOCKUP}/data/designs-data.js"


def ledger_script(root: Path | None = None) -> str:
    """The ledger as the script the v2 page loads in place of `data/designs-data.js`."""
    return "window.DESIGNS = %s;\n" % json.dumps(ledger(root), ensure_ascii=False, indent=1)


def read(target: Path, root: Path | None = None) -> bytes:
    """What the tab serves for a resolved file: its bytes, or the live ledger for the committed one."""
    base = Path(ROOT if root is None else root).expanduser()
    if target == (base / LIVE_LEDGER).resolve():
        return ledger_script(base).encode("utf-8")
    return target.read_bytes()


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
