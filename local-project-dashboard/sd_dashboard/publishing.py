"""Publishing (sd:5102): where a brief or a document was published, and the per-document Publish to choice.

The operator asked on 2026-10-18 for a choice to publish briefs and repository documents to Google Drive, Notion and
Obsidian, per-repo locations, and an icon on each row that opens the published copy. The command pack already holds
the mechanism (`skills/_shared/references/publication-contract.md`); this module reads and drives it and adds no
second queue, designation store or receipt format.

**Where a copy is read from.** `copies` gives one entry per destination, in `CHANNELS` order, for a document row
(`document_copies`) or a brief row (`brief_copies`):

- Notion and Drive: the queue request `<key>.<out>.<destination>.json` (the pack's `QUEUE`) says a copy is queued; the
  receipt beside it (`receipts()`) holds the URL the last drain delivered. Both must name this checkout as `repo`: the
  file name is keyed on a basename two checkouts may share.
- Any destination: the item's `fields.published` (`channels.record_url`), for a document whose research.conf.py entry
  names `item=` and for every brief.
- Obsidian: the vault copy itself, at `<vault>/<folder>/<out>.md`, where the folder is the repo's `channels` setting
  or `Briefs/<checkout name>` (the pack's `vault_folder`); for a brief, the link the Briefs reader already vouches for.

An entry is `{"channel", "url", "state", "title"}`: `state` is `published`, or `queued` while a request waits (with the
last delivered URL, if any). A channels setting that cannot be read leaves Obsidian out and says why in `unread`:
unknown is not "no copy".

**The choice.** A research-kit document's designation is its research.conf.py entry's `notion=` or `drive=` key
(contract § Outward destinations). `choices` reads which entries carry one without running the file: a key present
with any value but `None` or `False` is designated. Obsidian is not per document: the repo's channels setting turns it
on or off, so the page shows it and links to Management > Publishing.

`designate` adds one key, in four steps, and is idempotent:

1. The research.conf.py bytes must be ones a render by hand executed (`conf_trusted`): the dashboard vouches only for
   its own edit on top of a file the operator ran. Else it refuses and names `sd-research-kit render`.
2. The entry must be a literal `dict(...)` or `{...}` in a literal `DOCS` list whose `out` is a string; else it refuses
   and says to add the key by hand. A key already there skips to step 4.
3. The text gains `<channel>=dict()` (or `"<channel>": {}`) after the entry's last item. The new text must parse to the
   old tree plus that one key; it replaces the file atomically, and its digest becomes the trusted one.
4. `sd-research-kit publish` runs in the checkout, as an automatic run (`SD_RESEARCH_TRIGGER=dashboard`), so it checks
   the trust record against the bytes it executes and queues through the pack's own path. Its lines are the answer.

Removing a designation is refused: the entry may hold the page or file id a drain wrote, and dropping it orphans the
copy. The operator edits the file.

**A brief's choice** (ruling on sd:5102, note #23223) is `fields.publish.<channel>` on its item: `designate_brief` writes
it in one transaction and calls the pack's `enqueue_briefs`, which builds the request from the row and queues it as for a
research document; every brief render queues it again when its text changes.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

__all__ = ["CHANNELS", "OUTWARD", "PUBLISH_SECONDS", "brief_choice", "brief_copies", "brief_request", "choices", "choices_of",
           "designate", "designate_brief", "document_copies", "item_fields", "published_fields", "request"]

#: The destinations, in the order a row shows their icons.
CHANNELS = ("drive", "notion", "obsidian")
#: The destinations a document designates; Obsidian is the repo's setting, not the document's.
OUTWARD = ("notion", "drive")
#: The bound on `sd-research-kit publish`, which registers, writes vault copies and queues; it renders nothing.
PUBLISH_SECONDS = 120
#: What the hook-style trigger is called when the dashboard runs `publish`.
TRIGGER = "dashboard"
#: How a page names each destination.
NAMES = {"drive": "Google Drive", "notion": "Notion", "obsidian": "Obsidian"}


def kit():
    """The pack's `sd_research_publish`, from the `bin/` beside the loaded `sd_db` (`pack_module`)."""
    from . import pack_module

    return pack_module("sd_research_publish")


def repo_key(checkout: Path) -> str:
    """The queue's key for a main checkout: the pack's `repo_key`, without its git call (a documents root is a main
    checkout under `REPO_ROOT`, so its own name is the repository's)."""
    stem = re.sub(r"[^a-z0-9._-]+", "-", checkout.name.lower()).strip("-.")
    return stem or "docs"


def _same(path, checkout: Path) -> bool:
    try:
        return isinstance(path, str) and Path(path).resolve() == checkout.resolve()
    except OSError:
        return False


def _vault_name() -> Path:
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config

    return system_tools_config.obsidian_vault()


def obsidian_link(vault: Path, relative: str) -> str:
    """The pack's `record_obsidian` link: the vault's name and the note's path without `.md`."""
    return "obsidian://open?vault=%s&file=%s" % (urllib.parse.quote(vault.name, safe=""),
                                                 urllib.parse.quote(relative, safe=""))


def vault_folder(checkout: Path, setting) -> str | None:
    """The folder under the vault the render writes this repo's copies into, or None when Obsidian is off."""
    if setting is not None and "obsidian" not in setting:
        return None
    return (setting or {}).get("obsidian", {}).get("folder") or f"Briefs/{checkout.name}"


def read_channels(connection, checkout: Path):
    """`(setting, "")` with the repo's publish channels (None for no setting), or `(None, why)` when not read."""
    from sd_db import channels, repos

    if connection is None:
        return None, "no database connection, so the repo's channels setting was not read"
    try:
        return channels.outbound(repos.repo_channels(connection, str(checkout))), ""
    except Exception as error:  # noqa: BLE001 - a setting not read is unknown, never the defaults
        return None, f"the channels setting of {checkout.name} was not read: {error}"


def published_fields(connection, items) -> dict[int, dict]:
    """Each item's `fields.published`, by id; an item with none, or fields that are not JSON, has `{}`."""
    return {item: value if isinstance(value := fields.get("published"), dict) else {}
            for item, fields in item_fields(connection, items).items()}


def _http(url) -> bool:
    return isinstance(url, str) and urllib.parse.urlsplit(url).scheme in ("http", "https")


def _openable(channel: str, url) -> bool:
    """Only a link the page may open: http(s) for Notion and Drive, `obsidian://open` for the vault."""
    if channel == "obsidian":
        return isinstance(url, str) and url.startswith("obsidian://open?")
    return _http(url)


def _entry(channel: str, url, queued: bool) -> dict | None:
    url = url if _openable(channel, url) else None
    if not url and not queued:
        return None
    title = NAMES[channel] + (": queued, not yet drained" if queued else "")
    return {"channel": channel, "url": url, "state": "queued" if queued else "published", "title": title}


def _from_fields(published: dict, keys=None) -> dict[str, str]:
    """Channel -> URL from `fields.published`, for the entries `keys` names (every entry when None)."""
    urls: dict[str, str] = {}
    for key, entry in sorted(published.items()):
        if (keys is None or key in keys) and isinstance(entry, dict):
            for channel in CHANNELS:
                if isinstance(entry.get(channel), str):
                    urls.setdefault(channel, entry[channel])
    return urls


def document_copies(checkout: Path, out: str, *, setting, unread: str, fields: dict | None = None,
                    vault: Path | None = None, queue: Path | None = None, receipts: Path | None = None) -> dict:
    """The row's `pub.copies` for a research document: queue, receipt, item fields and the vault copy."""
    key = repo_key(checkout)
    from_item = _from_fields(fields or {}, {k for k in (fields or {}) if k.endswith(f"::{out}")})
    copies = []
    for channel in CHANNELS:
        if channel == "obsidian":
            url = from_item.get("obsidian")
            folder = None if unread else vault_folder(checkout, setting)
            if not url and folder and vault and (vault / folder / f"{out}.md").is_file():
                url = obsidian_link(vault, f"{folder}/{out}")
            entry = _entry(channel, url, False)
        else:
            name = f"{key}.{out}.{channel}.json"
            pending = _record(queue / name) if queue else {}
            receipt = _record(receipts / name) if receipts else {}
            queued = bool(pending) and _same(pending.get("repo"), checkout)
            delivered = receipt.get("url") if receipt.get("delivered") and _same(receipt.get("repo"), checkout) else None
            entry = _entry(channel, delivered or from_item.get(channel), queued)
        if entry:
            copies.append(entry)
    return {"copies": copies, "unread": unread}


def brief_copies(published: dict, open_link: str | None, queued=()) -> dict:
    """The row's `pub.copies` for a routine brief: its item's `fields.published`, the vault copy Briefs vouches for, and
    the channels `queued` names (a pending request in the pack's queue)."""
    urls = _from_fields(published)
    if open_link:
        urls["obsidian"] = open_link
    return {"copies": [entry for channel in CHANNELS if (entry := _entry(channel, urls.get(channel), channel in queued))],
            "unread": ""}


def brief_queued(checkout: Path, stem: str, queue: Path | None) -> set[str]:
    """The channels a brief has a pending request for, under this checkout's key and naming this checkout."""
    if queue is None:
        return set()
    return {channel for channel in OUTWARD
            if _same(_record(queue / f"{repo_key(checkout)}.{stem}.{channel}.json").get("repo"), checkout)}


def _record(path: Path) -> dict:
    """The JSON object at `path`, or `{}` for none, an unreadable file or anything else."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# -- the choice: research.conf.py designations -------------------------------------------------------------------


class Refused(ValueError):
    """A designation the dashboard will not write; nothing changed."""


def _docs_list(tree: ast.Module):
    """The literal list `DOCS` is assigned, or None when DOCS is built in code."""
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DOCS" for t in node.targets):
            found = node.value
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and getattr(node.target, "id", None) == "DOCS":
            return None
    return found if isinstance(found, ast.List) else None


def _items(entry) -> dict[str, ast.AST] | None:
    """A literal entry's keys and value nodes: `dict(k=v, ...)` or `{"k": v, ...}`; None for anything else."""
    if isinstance(entry, ast.Call) and isinstance(entry.func, ast.Name) and entry.func.id == "dict" and not entry.args:
        if any(k.arg is None for k in entry.keywords):
            return None
        return {k.arg: k.value for k in entry.keywords}
    if isinstance(entry, ast.Dict):
        if any(not (isinstance(k, ast.Constant) and isinstance(k.value, str)) for k in entry.keys):
            return None
        return {k.value: v for k, v in zip(entry.keys, entry.values)}
    return None


def _on(node) -> bool:
    """A designation key's value: anything but the constant None or False designates."""
    return not (isinstance(node, ast.Constant) and node.value in (None, False))


def _entries(source: str):
    """`(tree, {out: entry node})` for the literal entries, or `(None, why)` when the file cannot be read this way."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return None, f"research.conf.py does not parse: {error.msg} (line {error.lineno})"
    entries = _entries_of(tree)
    if entries is None:
        return None, "research.conf.py builds DOCS in code; add the key to the entry by hand"
    return tree, entries


def _entries_of(tree: ast.Module) -> dict | None:
    """`{out: entry node}` for the literal entries of a parsed file, or None when DOCS is built in code."""
    listed = _docs_list(tree)
    if listed is None:
        return None
    entries = {}
    for entry in listed.elts:
        items = _items(entry)
        out = items.get("out") if items else None
        if isinstance(out, ast.Constant) and isinstance(out.value, str):
            entries[out.value.strip()] = entry
    return entries


def choices_of(source: str) -> dict[str, dict[str, bool]]:
    """`{out: {"notion": bool, "drive": bool}}` for each literal entry; `Refused` when the text cannot be read this way."""
    tree, entries = _entries(source)
    if tree is None:
        raise Refused(entries)
    return {out: {channel: channel in items and _on(items[channel]) for channel in OUTWARD}
            for out, entry in entries.items() if (items := _items(entry)) is not None}


def choices(checkout: Path) -> dict:
    """`{"docs": choices_of(...), "why": ""}`; a file this cannot read gives `docs` {} and why."""
    try:
        return {"docs": choices_of((checkout / "research.conf.py").read_text(encoding="utf-8")), "why": ""}
    except (OSError, UnicodeDecodeError) as error:
        return {"docs": {}, "why": f"research.conf.py was not read: {error}"}
    except Refused as refused:
        return {"docs": {}, "why": str(refused)}


def _offset(source: str, line: int, column: int) -> int:
    """The character offset of an AST position (1-based line, 0-based UTF-8 byte column)."""
    lines = source.splitlines(keepends=True)
    head = "".join(lines[:line - 1])
    return len(head) + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))


def add_key(source: str, out: str, channel: str) -> str:
    """`source` with `channel` designated on the entry `out`; `Refused` when it cannot be done safely.

    The result must parse to the same tree plus the one key, or nothing is returned.
    """
    if channel not in OUTWARD:
        raise Refused(f"{channel!r} is not a destination a document designates; expected notion or drive")
    tree, entries = _entries(source)
    if tree is None:
        raise Refused(entries)
    entry = entries.get(out)
    if entry is None:
        raise Refused(f"research.conf.py has no literal entry with out={out!r}; add the key to it by hand")
    items = _items(entry)
    if channel in items:
        raise Refused(f"{out} already carries {channel}=" + ("" if _on(items[channel]) else
                      ", switched off; edit research.conf.py to turn it on"))
    last = (entry.keywords[-1].value if isinstance(entry, ast.Call) else entry.values[-1]) if items else None
    if last is None:
        raise Refused(f"the entry {out} is empty")
    first = entry.keywords[-1] if isinstance(entry, ast.Call) else entry.keys[-1]
    indent = " " * first.col_offset if first.lineno != entry.lineno else ""
    text = f"{channel}=dict()" if isinstance(entry, ast.Call) else f'"{channel}": {{}}'
    at = _offset(source, last.end_lineno, last.end_col_offset)
    joined = f",\n{indent}{text}" if indent else f", {text}"
    result = source[:at] + joined + source[at:]
    _check(tree, result, out, channel)
    return result


def _check(tree: ast.Module, result: str, out: str, channel: str) -> None:
    """Refuse a result that is not the old tree plus the one key."""
    try:
        after = ast.parse(result)
    except SyntaxError as error:
        raise Refused(f"the edit would not parse ({error.msg}); add the key by hand") from None
    entries = _entries_of(after)
    entry = entries.get(out) if entries else None
    if entry is None:
        raise Refused("the edit lost its entry; add the key by hand")
    if isinstance(entry, ast.Call):
        entry.keywords = [k for k in entry.keywords if k.arg != channel]
    else:
        kept = [(k, v) for k, v in zip(entry.keys, entry.values) if k.value != channel]
        entry.keys, entry.values = [k for k, _ in kept], [v for _, v in kept]
    if ast.dump(after) != ast.dump(tree):
        raise Refused("the edit changed more than the one key; add the key by hand")


def _write(path: Path, text: str) -> None:
    partial = path.with_name(path.name + ".dashboard-partial")
    partial.write_text(text, encoding="utf-8")
    os.chmod(partial, path.stat().st_mode & 0o7777)
    os.replace(partial, path)


def _publish(checkout: Path, runner=subprocess.run) -> tuple[int, list[str]]:
    """`sd-research-kit publish` in the checkout, as an automatic run; its exit and its lines."""
    from . import pack_bin

    environment = {**os.environ, "SD_RESEARCH_TRIGGER": TRIGGER}
    for name in ("SD_MIRROR_DRY_RUN", "SD_PUBLISH_FROM_WORKTREE", "SD_MIRROR_REQUEUE"):
        environment.pop(name, None)
    try:
        done = runner([sys.executable, str(pack_bin() / "sd-research-kit"), "publish"], cwd=str(checkout),
                      env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=PUBLISH_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return 124, [f"sd-research-kit publish gave no answer in {PUBLISH_SECONDS} s; whether it queued is not known"]
    except OSError as error:
        return 127, [f"sd-research-kit publish did not start: {error}"]
    return done.returncode, [line for line in (done.stdout + done.stderr).splitlines() if line.strip()]


def designate(checkout: Path, out: str, channel: str, *, runner=subprocess.run) -> dict:
    """Steps 1 to 4 above: `{"designated", "queued", "lines"}`, or `Refused` with nothing changed."""
    conf = checkout / "research.conf.py"
    publish = kit()
    try:
        before = conf.read_bytes()
    except OSError as error:
        raise Refused(f"research.conf.py was not read: {error}") from None
    if not publish.conf_trusted(checkout, before):
        raise Refused(f"no render by hand has executed this research.conf.py; read it, run `sd-research-kit render` in "
                      f"{checkout}, then tick again")
    source = before.decode("utf-8")
    tree, entries = _entries(source)
    items = _items(entries[out]) if tree is not None and out in entries else None
    already = bool(items) and channel in items and _on(items[channel])
    if not already:
        after = add_key(source, out, channel).encode("utf-8")
        if conf.read_bytes() != before:
            raise Refused("research.conf.py changed while the dashboard read it; tick again")
        _write(conf, after.decode("utf-8"))
        publish.trust_conf(checkout, after)
    status, lines = _publish(checkout, runner)
    # The pack's report lines: `mirror: queued <out> -> ...`, or `mirror: <out>: already queued -> ...` (`enqueue`).
    queued = any(line.startswith((f"mirror: queued {out} ", f"mirror: {out}: already queued")) for line in lines)
    return {"designated": channel, "already": already, "queued": queued and status == 0, "exit": status, "lines": lines[-12:]}


def request(values: dict, *, config_path=None, repo_root=None):
    """The POST `/api/documents/publish` checks: `{root, file, channel}` naming a research document the page lists."""
    from . import documents, documents_screen

    if set(values) != {"root", "file", "channel"} or not all(isinstance(values[k], str) for k in values):
        raise ValueError("Send the document's root and file, and the channel: notion or drive.")
    if values["channel"] not in OUTWARD:
        raise ValueError("A document designates notion or drive; Obsidian is the repo's channels setting.")
    if documents.resolve(values["root"], values["file"], config_path, repo_root) is None:
        raise ValueError(f"No document {values['root']}/{values['file']} is served: read the documents again.")
    root = next((r for r in documents.roots(config_path, repo_root) if r.key == values["root"]), None)
    checkout = documents_screen._checkout(root) if root else None
    if checkout is None or not (checkout / "research.conf.py").is_file():
        raise ValueError("Only a research-kit document has a designation: its checkout holds no research.conf.py.")
    # The page is the entry's `out` name plus `.html`, as `documents_screen._sources` matches them.
    outs = [out for out in choices(checkout)["docs"] if f"{Path(out).name}.html" == values["file"]]
    if len(outs) != 1:
        raise ValueError(f"research.conf.py has no single literal entry rendering {values['file']}; add the key by hand.")
    out = outs[0]

    def write(connection):
        try:
            return designate(checkout, out, values["channel"])
        except Refused as refused:
            raise ValueError(str(refused)) from None

    return write


# -- a routine brief's designation: its item's fields.publish (ruling on sd:5102, note #23223) ----------------------


def item_fields(connection, items) -> dict[int, dict]:
    """Each item's whole `fields` object, by id; fields that are not a JSON object read as `{}`."""
    ids = sorted({item for item in items if type(item) is int})
    if connection is None or not ids:
        return {}
    out = {}
    for row in connection.execute(f"SELECT id, fields FROM item WHERE id IN ({','.join('?' * len(ids))})", ids):
        try:
            fields = json.loads(row[1] or "{}")
        except ValueError:
            fields = {}
        out[row[0]] = fields if isinstance(fields, dict) else {}
    return out


def brief_choice(fields: dict, setting, unread: str) -> dict:
    """A brief's Publish to boxes: its `fields.publish` keys, and Obsidian from the repo's setting (None when unread)."""
    publish = fields.get("publish") if isinstance(fields.get("publish"), dict) else {}
    return {"notion": isinstance(publish.get("notion"), dict), "drive": isinstance(publish.get("drive"), dict),
            "obsidian": None if unread else setting is None or "obsidian" in setting, "why": ""}


def _brief(connection, item: int):
    """`(row, checkout, fields)` for a routine brief, or `Refused`."""
    from sd_db import paths

    row = connection.execute("SELECT id, title, repo, source, kind, body, fields FROM item WHERE id = ?", (item,)).fetchone()
    if row is None or row["source"] != "brief" or row["kind"] != "report":
        raise Refused(f"sd:{item} is not a routine brief")
    if not row["title"] or "/" in row["title"] or row["title"].startswith("."):
        raise Refused(f"sd:{item}'s title is no file name, so its copy has no snapshot or request name")
    checkout = paths.expand(row["repo"]) if row["repo"] else None
    if checkout is None or not checkout.is_dir():
        raise Refused(f"sd:{item} names no checkout on this machine, so its mirror has no repository to queue under")
    fields = item_fields(connection, [item]).get(item, {})
    return row, checkout, fields


def designate_brief(connection, item: int, channel: str, *, enqueue=None) -> dict:
    """Publish to on a brief, in three steps, idempotent like `designate`:

    1. The repo's channels setting must be read and must not leave the channel out; else it refuses, nothing written.
    2. `fields.publish.<channel>` becomes `{}` in one transaction, unless it is there already.
    3. The pack's `enqueue_briefs` queues the brief's designated channels from its row; a pack without it says so.
    """
    from sd_db import writes
    from sd_db.database import transaction

    if channel not in OUTWARD:
        raise Refused(f"{channel!r} is not a destination a brief designates; expected notion or drive")
    row, checkout, fields = _brief(connection, item)
    setting, unread = read_channels(connection, checkout)
    if unread:
        raise Refused(f"{unread}; nothing was designated")
    if setting is not None and channel not in setting:
        raise Refused(f"{channel} is not a channel of {checkout.name}: turn it on under Management > Publishing")
    publish = fields.get("publish") if isinstance(fields.get("publish"), dict) else {}
    already = isinstance(publish.get(channel), dict)
    if not already:
        with transaction(connection):
            raw = connection.execute("SELECT fields FROM item WHERE id = ?", (item,)).fetchone()[0]
            try:
                current = json.loads(raw or "{}")
            except ValueError:
                current = None
            if not isinstance(current, dict):
                raise Refused(f"sd:{item}'s fields are not a JSON object; nothing was designated, so nothing in them is lost")
            held = current.get("publish") if isinstance(current.get("publish"), dict) else {}
            fields = {**current, "publish": {**held, channel: {}}}
            writes.set_item_fields(connection, item, fields=fields)
    try:
        queue = enqueue or getattr(kit(), "enqueue_briefs", None)
        if queue is None:
            return {"designated": channel, "already": already, "queued": False, "exit": 1,
                    "lines": ["the installed command pack has no enqueue_briefs (sd:5102), so nothing was queued; update it"]}
        lines = queue(connection, {item})
    except Exception as error:  # noqa: BLE001 - the designation stands; the next tick or brief render queues again
        return {"designated": channel, "already": already, "queued": False, "exit": 1,
                "lines": [f"the pack's enqueue failed, so nothing was queued: {error}"]}
    stem = row["title"]
    queued = any(line.startswith((f"mirror: queued {stem} ", f"mirror: {stem}: already queued")) for line in lines)
    return {"designated": channel, "already": already, "queued": queued, "exit": 0, "lines": lines[-12:]}


def brief_request(values: dict):
    """The POST `/api/briefs/publish` checks: `{item, channel}`; the designation as a write."""
    if set(values) != {"item", "channel"} or type(values["item"]) is not int or not isinstance(values["channel"], str):
        raise ValueError("Send the brief's item and the channel: notion or drive.")
    if values["channel"] not in OUTWARD:
        raise ValueError("A brief designates notion or drive; Obsidian is the repo's channels setting.")

    def write(connection):
        try:
            return designate_brief(connection, values["item"], values["channel"])
        except Refused as refused:
            raise ValueError(str(refused)) from None

    return write
