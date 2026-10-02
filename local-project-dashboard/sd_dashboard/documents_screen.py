"""Documents: the rows behind the default UI's Documents page (sd:2114).

The page is `v2/documents.html`; it holds no rows. It reads one JSON document,
`/api/documents`, built here by `document` from the readers the classic tab
uses (`documents.roots`, `documents.documents`, `documents.unresolved`), so
the page lists exactly the files `/documents/<key>/<file>` serves.

**What one row says.** The listing's facts (file, bytes, modified) and four
read from the file itself: its first `<title>`, its first `<h1>`, and its stand
line (`<meta name="description">`, else the first element whose class is
`standfirst`, `lede`, `stand` or `dek`). Only the first `HEAD_BYTES` are read:
a map page runs to megabytes, and its title is at the top.

**Kind is derived.** A root whose checkout holds `research.conf.py` gives
research documents; every other root gives reports. No reader can tell a
dashboard or a design document yet, so the page shows those kinds with a count
of 0 and says why.

**Render-stale.** A research document is render-stale when its source
Markdown changed after the file in the root. `_sources` finds the source: the
checkout's `research.conf.py` (`src` and `out`, read with the collectors'
`read_research_config`, which never runs repository code), else one `.md`
with the page's name. A file with no source has an unknown render state, and
the page says so.

**No view state.** Pins, hidden documents and tags need a store that does not
exist (docs/work/2026-09-28-documents-view-state). `store.available` is false
and `store.reason` says so; nothing here writes.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import timezone
from html.parser import HTMLParser
from pathlib import Path

from . import documents

__all__ = ["HEAD_BYTES", "STORE_REASON", "document", "facts"]

#: How much of a file the title, h1 and stand line are read from.
HEAD_BYTES = 256 * 1024

#: Why pin, hide and tag are off: the one reason the page shows for each.
STORE_REASON = "no document store yet: pins, hidden documents and tags are not kept (docs/work/2026-09-28-documents-view-state)"

#: How deep under a research checkout a source Markdown is looked for, and the folders that never hold one.
SOURCE_DEPTH = 3
SKIP_FOLDERS = ("build", "node_modules", "dist", "storage")

#: Classes that mark a page's stand line, in the order the generators use them.
STAND = ("standfirst", "lede", "stand", "dek")


class _Head(HTMLParser):
    """The first title, h1 and stand line, as text; it stops looking once it has all three."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = {"title": None, "h1": None, "desc": None}
        self.open: str | None = None  # which fact the text is for
        self.element = ""  # the element that holds it; a nested one of the same name is counted
        self.text: list[str] = []
        self.depth = 0

    def handle_starttag(self, name, attributes):
        attributes = dict(attributes)
        if self.open:
            self.depth += name == self.element
            return
        if name == "meta" and (attributes.get("name") or "").lower() == "description" and self.found["desc"] is None:
            self.found["desc"] = " ".join((attributes.get("content") or "").split())
            return
        if name in ("svg", "math") and self.found["title"] is None:
            # An inline chart carries its own <title> elements; none of them names the page.
            self.found["title"] = ""
        if name == "title" and self.found["title"] is None:
            self.open = "title"
        elif name == "h1" and self.found["h1"] is None:
            self.open = "h1"
        elif self.found["desc"] is None and set((attributes.get("class") or "").split()) & set(STAND):
            self.open = "desc"
        if self.open:
            self.element, self.text, self.depth = name, [], 0

    def handle_endtag(self, name):
        if not self.open or name != self.element:
            return
        if self.depth:
            self.depth -= 1
            return
        self.found[self.open] = " ".join("".join(self.text).split())
        self.open = None

    def handle_data(self, data):
        if self.open:
            self.text.append(data)


def facts(path: Path) -> dict:
    """The title, h1 and stand line of one file; an empty string for each one it does not have."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return {"title": "", "h1": "", "desc": ""}
    parser = _Head()
    try:
        parser.feed(head)
    except (AssertionError, ValueError):
        pass
    return {key: value or "" for key, value in parser.found.items()}


def _collectors():
    # `read_research_config` lives in the collectors this dashboard owns, read by path as `ports_screen.py` reads them.
    path = Path(__file__).resolve().parents[1] / "collectors.py"
    spec = importlib.util.spec_from_file_location("sd_dashboard_document_collectors", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("the research config reader is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _checkout(root: documents.Root) -> Path | None:
    """The checkout a root publishes from, when the root is its `docs/dashboard`."""
    path = root.path.expanduser()
    parts = Path(documents.PUBLISHED).parts
    return path.parents[len(parts) - 1] if path.parts[-len(parts):] == parts else None


def _sources(checkout: Path, names: list[str]) -> dict[str, Path]:
    """Each file name -> the source Markdown it renders from, for the names that have one.

    research.conf.py first: an entry's `out` names the page and its `src` the source, when that is a file. Then, for a
    name the config did not give, a `.md` whose stem is the page's stem in any case (`40-docs/PLAN-local-poc.md` for
    `plan-local-poc.html`), when exactly one does. A config the reader refuses (computed data) leaves the name match, and is not a failure of the page.
    """
    base = checkout.resolve()
    found: dict[str, Path] = {}
    try:
        config = _collectors().read_research_config(checkout / "research.conf.py")
    except Exception:  # noqa: BLE001 - the name match below still answers
        config = {}
    for entry in config.get("DOCS", []):
        source = checkout / entry["src"]
        name = f"{Path(entry['out']).name}.html"
        # Only a source file that exists: a missing or folder `src` leaves the name match, else an unknown render state.
        if name in names and source.is_file() and source.resolve().is_relative_to(base):
            found.setdefault(name, source)
    stems: dict[str, list[Path]] = {}
    for directory, folders, files in os.walk(checkout):
        here = Path(directory)
        folders[:] = sorted(f for f in folders if not f.startswith(".") and f not in SKIP_FOLDERS
                            and len((here / f).relative_to(checkout).parts) <= SOURCE_DEPTH
                            and (here / f).relative_to(checkout).as_posix() != documents.PUBLISHED)
        for file in files:
            if file.endswith(".md"):
                stems.setdefault(file[:-3].lower(), []).append(here / file)
    for name in names:
        matches = stems.get(name[:-5].lower(), [])
        if name not in found and len(matches) == 1 and matches[0].resolve().is_relative_to(base):
            found[name] = matches[0]
    return found


def _tilde(path: Path) -> str:
    home = Path.home()
    return "~/" + path.relative_to(home).as_posix() if path.is_relative_to(home) else str(path)


def _iso(moment) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def document(*, now: str, config_path: Path | None = None, repo_root: Path | None = None) -> dict:
    """The Documents page's document: every root, every file the server serves, and what was not read."""
    rows: list[dict] = []
    listed: list[dict] = []
    for root in documents.roots(config_path, repo_root):
        checkout = _checkout(root)
        research = bool(checkout and (checkout / "research.conf.py").is_file())
        found = documents.documents(root)
        sources = _sources(checkout, [entry["name"] for entry in found]) if research else {}
        listed.append({"key": root.key, "label": root.label, "path": _tilde(root.path.expanduser()),
                       "n": len(found), "research": research})
        for entry in found:
            source = sources.get(entry["name"])
            try:
                stale = bool(source and source.stat().st_mtime > entry["modified"].timestamp())
            except OSError:
                stale = False
            rows.append({
                "key": root.key, "file": entry["name"], "href": entry["href"], "bytes": entry["bytes"],
                "modified": _iso(entry["modified"]), "kind": "research" if research else "report",
                "src": source.relative_to(checkout).as_posix() if source else "", "stale": stale,
                **facts(root.path.expanduser() / entry["name"]),
            })
    rows.sort(key=lambda row: row["modified"], reverse=True)
    contested = [{"key": key, "label": documents._label(key), "paths": [_tilde(path) for path in paths]}
                 for key, paths in documents.unresolved(config_path, repo_root).items()]
    return {
        "read": now,
        "base": _tilde(documents._base(repo_root)),
        "published": documents.PUBLISHED,
        "config": "<config>/project-dashboard/documents.conf",
        "roots": listed,
        "contested": contested,
        "store": {"available": False, "reason": STORE_REASON},
        "documents": rows,
    }
