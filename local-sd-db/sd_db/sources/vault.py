"""The vault notes a plugin declares, as `item` rows of kind `idea`.

One hundred and seventy-nine ideas and eleven topics on 2026-09-05, each an
Obsidian note with a frontmatter block. Every row lands as kind `idea`,
because that is the kind the schema names; which plugin kind a row actually
is stays in `fields`, where the manifest that declared it can read it back.

**The plugin declares what is imported (sd:1425).** Each manifest's
`workflow` block names the kinds to import, maps every ladder word of each to
one of the six item statuses, and may name the field that holds a due date.
The importer asks the installed pack for them through
`sd plugin list --json`, and trusts only what that command has validated: a
second validator here would be a copy of the pack's rules in another
repository. A plugin with no block imports nothing.

**`stage` keeps the ladder word verbatim.** Criterion 5 compares the set of
stages in the rows with the set in the frozen source, so a row that stored
`in_progress` and forgot `drafting` would pass a comparison it was never
asked. `item.status` beside it is the declared map's answer to the same
note: the ladder is the plugin's, the six statuses are the system's.

**Every failure refuses the whole sitting and writes nothing.** A word the
map does not name, a due value that is not a date, two plugins declaring one
kind, a plugin the pack could not read: each stops the import and names what
is at fault. A partial import that skipped one plugin would be the silent
failure the operator ruled out, and so would an import of zero kinds.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import pack
from ..writes import promoted_rows, upsert_item
from . import Counts, Frozen, MigrationRefused, Record, digest
from .frontmatter import read as read_frontmatter

#: `source` on every row this migration writes.
SOURCE = "vault"

WHO = "vault migration"

#: Where the vault is. `$OBSIDIAN_VAULT`, else a default under `~/Documents`.
DEFAULT_VAULT = "~/Documents/Obsidian Vault"

#: How long `sd plugin list --json` may take. The pack bounds each root's read
#: at five seconds, so this is far past any healthy answer.
LIST_SECONDS = 120

DUE = re.compile(r"\d{4}-\d{2}-\d{2}")


def vault_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(os.path.expanduser(env.get("OBSIDIAN_VAULT") or DEFAULT_VAULT))


def plugin_entries(*, home: Path | str, environ: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The registered plugins, as the installed pack's `sd plugin list --json` reports them.

    Run with this interpreter, as the design says: the pack's `bin/sd` needs
    nothing the library's own interpreter lacks.
    """
    env = dict(os.environ if environ is None else environ)
    found = pack.installed(home=home, environ=env)
    if found.checkout is None:
        raise MigrationRefused(f"the vault import reads the plugins through the pack, and {found.why}")
    command = found.checkout / "bin" / "sd"
    if not command.is_file():
        raise MigrationRefused(f"the vault import reads the plugins through the pack, and {command} does not exist")
    try:
        done = subprocess.run([sys.executable, str(command), "plugin", "list", "--json"],
                              capture_output=True, text=True, env=env, timeout=LIST_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise MigrationRefused(f"`{command} plugin list --json` did not answer in {LIST_SECONDS}s") from None
    if done.returncode != 0:
        first = (done.stderr.strip().splitlines() or ["no stderr"])[0]
        raise MigrationRefused(f"`{command} plugin list --json` exited {done.returncode}: {first}")
    try:
        entries = json.loads(done.stdout)
    except ValueError:
        entries = None
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise MigrationRefused(f"`{command} plugin list --json` printed what is not JSON entries")
    return entries


@dataclass(frozen=True)
class Base:
    """One declared kind: where its notes are and how its words map."""

    kind: str
    prefix: str
    root: Path
    relative: str
    status: dict[str, str]
    due: str | None = None


def declared_bases(entries: list[dict[str, Any]],
                   environ: dict[str, str] | None = None) -> tuple[tuple[Base, ...], list[str]]:
    """`(bases, notes)` from the `plugin list` entries, or a refusal naming what is wrong."""
    env = os.environ if environ is None else environ
    bases: dict[str, Base] = {}
    notes: list[str] = []
    for entry in entries:
        if not entry.get("readable"):
            raise MigrationRefused(f"the plugin at {entry.get('root')} is not readable: {entry.get('why')}")
        prefix = str(entry.get("prefix") or entry.get("root"))
        for key in ("manifestError", "dashboardError"):
            if entry.get(key):
                raise MigrationRefused(f"plugin {prefix}'s manifest has an error, so its declaration "
                                       f"cannot be known: {entry[key]}")
        workflow = entry.get("workflow") or {}
        if not workflow:
            notes.append(f"{prefix}: declares no dated kinds; nothing imported")
            continue
        store = entry["store"]
        variable = str(store["root"])[1:]
        if variable == "OBSIDIAN_VAULT":
            root = vault_root(env)
        elif env.get(variable):
            root = Path(os.path.expanduser(env[variable]))
        else:
            raise MigrationRefused(f"plugin {prefix} keeps its notes under ${variable}, which is not set")
        for kind, declared in sorted(workflow.items()):
            if kind in bases:
                raise MigrationRefused(f"plugins {bases[kind].prefix} and {prefix} both declare kind "
                                       f"{kind!r}; a row's identity carries the kind and not the prefix")
            bases[kind] = Base(kind=kind, prefix=prefix, root=root, relative=store["bases"][kind],
                               status=dict(declared["status"]), due=declared.get("due-field"))
    if not bases:
        raise MigrationRefused("no registered plugin declares a `workflow` kind, so the import "
                               "would read zero notes")
    return tuple(bases.values()), notes


@dataclass
class Reader:
    """The vault migration, over the kinds the registered plugins declare."""

    bases: tuple[Base, ...]
    name: str = SOURCE
    notes: list[str] = field(default_factory=list)

    @classmethod
    def at(cls, *, home: Path | str, environ: dict[str, str] | None = None) -> "Reader":
        return cls.from_plugins(plugin_entries(home=home, environ=environ), environ=environ)

    @classmethod
    def from_plugins(cls, entries: list[dict[str, Any]],
                     environ: dict[str, str] | None = None) -> "Reader":
        bases, notes = declared_bases(entries, environ)
        return cls(bases=bases, notes=notes)

    # ------------------------------------------------------------ freeze

    def freeze(self) -> Frozen:
        records: dict[str, Record] = {}
        notes: list[str] = list(self.notes)
        for base in self.bases:
            kind, directory = base.kind, base.root / base.relative
            if not directory.is_dir():
                raise MigrationRefused(
                    f"{directory} is not a directory; the vault's {kind} base is where "
                    f"plugin {base.prefix}'s `store.bases` says it is, and an absent one "
                    f"would import as zero notes"
                )
            found = sorted(directory.rglob("*.md"))
            notes.append(f"{SOURCE}: {len(found)} {kind} note(s) under {base.relative}")
            for path in found:
                matter, body = read_frontmatter(path.read_text(encoding="utf-8"))
                stage = str(matter.get("status") or "").strip()
                if not stage:
                    raise MigrationRefused(
                        f"{path} has no `status:` in its frontmatter; the ladder "
                        f"word is what `stage` keeps and there is nothing to keep"
                    )
                if stage not in base.status:
                    raise MigrationRefused(
                        f"{path} is at stage {stage!r}, which plugin {base.prefix}'s "
                        f"`workflow` map does not name for {kind}. Declare it in that "
                        f"manifest before the import writes a status it guessed."
                    )
                identity = f"{kind}:{path.relative_to(base.root).as_posix()}"
                fields = {"kind": kind, **{str(key): value for key, value in matter.items()}}
                records[identity] = Record(
                    identity=identity,
                    payload={
                        "title": str(matter.get("title") or path.stem).strip(),
                        "stage": stage,
                        "status": base.status[stage],
                        "created": str(matter.get("dateCreated") or "").strip(),
                        "due": _due(path, base, matter),
                        "fields": digest(fields),
                        "body": digest({"markdown": body}),
                    },
                )
        return Frozen(source=self.name, records=records, notes=notes)

    # ------------------------------------------------------------ import

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        counts = Counts()
        roots = {base.kind: base.root for base in self.bases}
        for identity in sorted(frozen.records):
            payload = frozen.records[identity].payload
            kind, _, relative = identity.partition(":")
            path = roots[kind] / relative
            matter, body = read_frontmatter(path.read_text(encoding="utf-8"))
            fields = {"kind": kind, **{str(key): value for key, value in matter.items()}}
            _, what = upsert_item(
                connection,
                source=SOURCE,
                external_id=identity,
                kind="idea",
                title=payload["title"],
                status=payload["status"],
                who=WHO,
                created_at=payload["created"] or None,
                due=payload["due"],
                path=relative,
                stage=payload["stage"],
                fields=fields,
                body={"markdown": body},
            )
            counts.record(what)
        return counts

    # ------------------------------------------------------------ verify

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        held: dict[str, Record] = {}
        found = [dict(row) for row in connection.execute(
            "SELECT * FROM item WHERE source = ? ORDER BY external_id", (SOURCE,)
        )]
        # A promoted idea answers with the row it was promoted from (sd:1994).
        for promoted in promoted_rows(connection, SOURCE):
            origin = json.loads(promoted["fields"])["promoted_from"]
            found.append({**origin["row"], "external_id": origin["external_id"]})
        for row in found:
            held[row["external_id"]] = Record(
                identity=row["external_id"],
                payload={
                    "title": row["title"],
                    "stage": row["stage"],
                    "status": row["status"],
                    "created": row["created_at"],
                    "due": row.get("due"),
                    "fields": digest(json.loads(row["fields"] or "{}")),
                    "body": digest(json.loads(row["body"] or "{}")),
                },
            )
        return held


def _due(path: Path, base: Base, matter: dict[str, Any]) -> str | None:
    """The declared due field as `YYYY-MM-DD`; absent or empty is `None`, anything else refuses."""
    if base.due is None:
        return None
    value = str(matter.get(base.due) or "").strip()
    if not value:
        return None
    try:
        if not DUE.fullmatch(value):
            raise ValueError(value)
        datetime.date.fromisoformat(value)
    except ValueError:
        raise MigrationRefused(f"{path} has `{base.due}` {value!r}, which plugin {base.prefix} "
                               f"declares as its due date and is not YYYY-MM-DD") from None
    return value


def stages_in(records: dict[str, Record]) -> set[str]:
    """Every ladder word a set of records holds. Criterion 5's set comparison."""
    return {str(record.payload.get("stage")) for record in records.values()}
