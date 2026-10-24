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
`read_research_inventory`, which never runs repository code), else one `.md`
with the page's name. A file with no source has an unknown render state, and
the page says so. A config that computes its data is read from the
`docs/dashboard/research.json` its last render wrote, as Research reads it
(sd:5666, sd:5659). Each research root says the refusal in `conf`, which names
`sd-research-kit render` when no manifest was written, and in `manifest`
whether it read `current` or `older` (a render of an earlier config).

**View state.** Pin, Hide and Tag are the workflow database's `view` state
(sd:3428, the pack's `sd_db.writes.set_view` and `views`), one row per file
keyed `<root key>/<file>`. Each row carries its `pinned`, `hidden` and `tags`.
A read that fails pins, hides and tags nothing: `store.available` is false and
`store.reason` says why, so the page shows every file and turns the commands
off. `request` is `POST /api/documents/view`, `{"root", "file"}` and any of
`pinned`, `hidden` and `tags`: a file the server does not serve is refused,
and the pack judges the values. A field left out keeps its value.

**Root views.** A root key's own `view` row (no `/`) names and hides the root
(sd:3986): its `label` replaces the derived name, and `hidden` moves the root
to the page's Hidden roots fold and its files out of the rows. Serving does
not change. Each root says its `label`, the `derived` name, the name its row
`set`, `hidden`, and `file_label`, a `label|` line documents.conf still holds,
which is not read. An unread store shows derived names and hides no root.
`request` with `{"root"}` and no `file` writes `label` or `hidden` for a root
the page lists.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from datetime import timezone
from html.parser import HTMLParser
from pathlib import Path

from sd_db import writes
from sd_db.errors import SdDbError

from . import documents, publishing

__all__ = ["HEAD_BYTES", "VIEW_FIELDS", "document", "facts", "request", "views"]

#: How much of a file the title, h1 and stand line are read from.
HEAD_BYTES = 256 * 1024

#: What a view write may set (sd:3428); a field left out keeps its value.
VIEW_FIELDS = ("pinned", "hidden", "tags")
#: What a root's view write may set (sd:3986).
ROOT_FIELDS = ("label", "hidden")

#: The folders under a research checkout that never hold a source Markdown; dot folders are skipped too.
SKIP_FOLDERS = ("build", "node_modules", "dist", "storage")

#: Each checkout's last walk (sd:3530): the mtime of every folder it listed, and the `.md` files by lower-case stem.
_walks: dict[Path, tuple[dict[Path, int], dict[str, list[Path]]]] = {}

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
            # An empty description says nothing, so the stand-class element still may.
            self.found["desc"] = " ".join((attributes.get("content") or "").split()) or None
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
    # `read_research_inventory` lives in the collectors this dashboard owns, read by path as `ports_screen.py` reads them.
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


def _inventory(checkout: Path) -> tuple[dict, str, str]:
    """research.conf.py's PROJECT and DOCS as Research reads them (sd:5666), the refusal, and "", "current" or "older"."""
    try:
        config, manifest = _collectors().read_research_inventory(checkout / "research.conf.py")
    except Exception as refused:  # noqa: BLE001 - the name match still answers; the root says why
        return {}, str(refused), ""
    return config, "", manifest


def _sources(checkout: Path, config: dict, names: list[str]) -> dict[str, Path]:
    """Each file name -> the source Markdown it renders from, for the names that have one.

    The config's DOCS first: an entry's `out` names the page and its `src` the source, when that is a file. Then, for a
    name the config did not give, a `.md` whose stem is the page's stem in any case (`40-docs/PLAN-local-poc.md` for
    `plan-local-poc.html`), when exactly one does anywhere in the checkout outside `SKIP_FOLDERS`, dot folders and the
    published root (sd:2417: a depth cap hid a unique deeper match, and a deeper twin could not make a match ambiguous). A config the reader refused is empty and leaves the name match, and is not a failure of the page.
    """
    base = checkout.resolve()
    found: dict[str, Path] = {}
    for entry in config.get("DOCS", []):
        source = checkout / entry["src"]
        name = f"{Path(entry['out']).name}.html"
        # Only a source file that exists: a missing or folder `src` leaves the name match, else an unknown render state.
        if name in names and source.is_file() and source.resolve().is_relative_to(base):
            found.setdefault(name, source)
    stems = _stems(checkout)
    for name in names:
        matches = stems.get(name[:-5].lower(), [])
        if name not in found and len(matches) == 1 and matches[0].resolve().is_relative_to(base):
            found[name] = matches[0]
    return found


def _stems(checkout: Path) -> dict[str, list[Path]]:
    """The checkout's `.md` files by lower-case stem, walked again only when a folder it listed changed (sd:3530).

    A file added, removed or renamed changes its folder's mtime, and a new folder its parent's, so one `stat` per
    folder answers whether the last walk still holds. Each folder is stat'ed before it is listed: a change during the
    walk leaves a newer mtime than the one kept, and the next read walks again. A walk that could not read a folder
    is not kept, so a folder that becomes readable is not missed.
    """
    kept = _walks.get(checkout)
    if kept is not None:
        try:
            if all(folder.stat().st_mtime_ns == mtime for folder, mtime in kept[0].items()):
                return kept[1]
        except OSError:
            pass
    seen: dict[Path, int] = {}
    stems: dict[str, list[Path]] = {}
    complete = True
    pending = [checkout]
    while pending:
        here = pending.pop()
        try:
            seen[here] = here.stat().st_mtime_ns
            with os.scandir(here) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            complete = False
            continue
        for entry in entries:
            path = here / entry.name
            try:
                folder = entry.is_dir()
            except OSError:
                complete = False
                continue
            if not folder:
                if entry.name.endswith(".md"):
                    stems.setdefault(entry.name[:-3].lower(), []).append(path)
            elif (not entry.name.startswith(".") and entry.name not in SKIP_FOLDERS and not entry.is_symlink()
                  and path.relative_to(checkout).as_posix() != documents.PUBLISHED):
                pending.append(path)
    if complete:
        _walks[checkout] = (seen, stems)
    return stems


def _tilde(path: Path) -> str:
    home = Path.home()
    return "~/" + path.relative_to(home).as_posix() if path.is_relative_to(home) else str(path)


def _iso(moment) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def views(connection) -> tuple[dict[str, dict], str]:
    """Every file's pin, hide and tags, keyed `<root key>/<file>`, and the reason when they could not be read."""
    if connection is None:
        return {}, "the view store was not read: no database connection"
    if not hasattr(writes, "views"):
        # A command pack from before sd:3430 has no view kind; pulling it is the remedy.
        return {}, "the command pack has no view store yet (sd:3430): pull it and restart the dashboard"
    try:
        return writes.views(connection), ""
    except (SdDbError, sqlite3.Error) as failure:
        return {}, f"the view store was not read: {failure}"


def request(payload: dict, *, config_path: Path | None = None, repo_root: Path | None = None):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if "file" not in payload:
        return _root_request(payload, config_path=config_path, repo_root=repo_root)
    if (not isinstance(payload.get("root"), str) or not isinstance(payload.get("file"), str)
            or set(payload) - {"root", "file", *VIEW_FIELDS} or not set(payload) & set(VIEW_FIELDS)):
        raise ValueError("Send the document's root and file, and pinned, hidden or tags.")
    if documents.resolve(payload["root"], payload["file"], config_path, repo_root) is None:
        raise ValueError(f"No document {payload['root']}/{payload['file']} is served: read the documents again.")
    if not hasattr(writes, "set_view"):
        raise ValueError("The command pack has no view store yet (sd:3430): pull it and restart the dashboard.")
    key = f"{payload['root']}/{payload['file']}"
    fields = {name: payload[name] for name in VIEW_FIELDS if name in payload}
    return lambda connection: {"key": key, **writes.set_view(connection, key, **fields)}


def _root_request(payload: dict, *, config_path: Path | None, repo_root: Path | None):
    """A root's rename or hide (sd:3986): a root the page lists, and `label` (text, empty clears) or `hidden`."""
    if (not isinstance(payload.get("root"), str) or set(payload) - {"root", *ROOT_FIELDS}
            or not set(payload) & set(ROOT_FIELDS) or not isinstance(payload.get("label", ""), str)):
        raise ValueError("Send the root, and its label or hidden.")
    if all(root.key != payload["root"] for root in documents.roots(config_path, repo_root)):
        raise ValueError(f"No document root {payload['root']} is listed: read the documents again.")
    key = payload["root"]
    fields = {name: payload[name] for name in ROOT_FIELDS if name in payload}
    return lambda connection: {"key": key, **writes.set_view(connection, key, **fields)}


def document(connection=None, *, now: str, config_path: Path | None = None, repo_root: Path | None = None) -> dict:
    """The Documents page's document: every root, every file the server serves, and what was not read."""
    kept, unread = views(connection)
    file_labels = documents.file_labels(config_path)
    rows: list[dict] = []
    listed: list[dict] = []
    for root in documents.roots(config_path, repo_root):
        checkout = _checkout(root)
        research = bool(checkout and (checkout / "research.conf.py").is_file())
        config, conf, manifest = _inventory(checkout) if research else ({}, "", "")
        found = documents.documents(root)
        own = kept.get(root.key, {})
        listed.append({"key": root.key, "label": own.get("label") or root.label, "derived": root.label,
                       "set": own.get("label"), "hidden": bool(own.get("hidden")), "file_label": file_labels.get(root.key),
                       "path": _tilde(root.path.expanduser()), "n": len(found), "research": research,
                       # sd:5958: the checkout a Request files its task under; "" for a root that is no checkout's.
                       "checkout": _tilde(checkout) if checkout else "",
                       "conf": conf, "manifest": manifest})
        if own.get("hidden"):
            continue
        sources = _sources(checkout, config, [entry["name"] for entry in found]) if research else {}
        publish = _publishing(connection, checkout, config) if research else None
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
                # Unread or never set: shown, unpinned, untagged, the pack's VIEW_CLEAR (spelled here for a pack without it).
                **kept.get(f"{root.key}/{entry['name']}", {"pinned": False, "hidden": False, "tags": []}),
                "pub": publish(entry["name"]) if publish else None,
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
        "store": {"available": not unread, "reason": unread},
        "documents": rows,
        "attention": _attention(rows),
    }


def _publishing(connection, checkout: Path, config: dict):
    """One research root's `pub` reader (sd:5102): each page's published copies and its Publish to choice.

    The channels setting, the designations, the queue and the vault are read once per root. A read that fails is named
    in `unread` or `choice.why`, never shown as no copy or no designation.
    """
    setting, unread = publishing.read_channels(connection, checkout)
    # sd:5500: the vault's own repository keeps Obsidian repo-wide; every other one designates it per document.
    whole = publishing.vault_repository(checkout)
    repo_wide = None if unread else setting is None or "obsidian" in setting
    picked = publishing.choices(checkout)
    try:
        kit = publishing.kit()
        queue, receipts = kit.QUEUE, kit.receipts()
    except ImportError as missing:
        queue = receipts = None
        unqueued = f"the pack's mirror queue was not read: {missing}"
    else:
        unqueued = ""
    try:
        vault = publishing._vault_name()
    except Exception:  # noqa: BLE001 - no vault name is no vault link; the other copies still show
        vault = None
    # A refused config is empty, and a manifest carries no item=: the receipts still answer.
    outs = {f"{Path(e['out']).name}.html": e["out"] for e in config.get("DOCS", [])}
    outs.update({f"{Path(out).name}.html": out for out in picked["docs"]})
    items = {e["out"]: e.get("item") for e in config.get("DOCS", []) if type(e.get("item")) is int}
    fields = publishing.published_fields(connection, items.values())

    def pub(name: str) -> dict:
        out = outs.get(name, name[:-5] if name.endswith(".html") else name)
        found = publishing.document_copies(checkout, out, setting=setting, unread=unread,
                                           fields=fields.get(items.get(out)), vault=vault, queue=queue, receipts=receipts)
        chosen = picked["docs"].get(out)
        why = picked["why"] or ("" if chosen else f"research.conf.py has no literal entry for {out}; add the key by hand")
        found["unread"] = "; ".join(cause for cause in (found["unread"], unqueued) if cause)
        return {**found, "choice": {"notion": bool(chosen and chosen["notion"]), "drive": bool(chosen and chosen["drive"]),
                                    "obsidian": repo_wide if whole else bool(chosen and chosen["obsidian"]),
                                    "repo_wide": publishing.VAULT_REPO if whole else "", "why": why}}

    return pub


def _attention(rows) -> dict:
    """The rail badge (sd:3446): documents whose source changed after their page, less the ones the operator hid."""
    from .v2.pages import attention

    return attention(("caution" if row["stale"] and not row.get("hidden") else "" for row in rows),
                     {"caution": "render-stale documents"})
