"""The vault's Blog Ideas and Topics, as `item` rows of kind `idea`.

One hundred and seventy-nine ideas and eleven topics on 2026-09-05, each an
Obsidian note with a frontmatter block. Both land as kind `idea` because
those are the kinds the schema names; which of the writing pack's kinds a row
actually is stays in `fields`, where the manifest that declared it can read
it back.

**`stage` keeps the ladder word verbatim.** Criterion 5 compares the set of
stages in the rows with the set in the frozen source, so a row that stored
`in_progress` and forgot `drafting` would pass a comparison it was never
asked. The `item.status` beside it is this table's answer to the same note,
and the two are different questions: the ladder is the writing pack's, the
six statuses are the system's.

**The table is the authority, and an unmapped word is a refusal.** A note
carrying a stage this table does not name stops the import and says the word.
Guessing a status for an unknown rung is how a vocabulary drifts into a
database and is never noticed again.

**The table carries every transition target in the writing manifest, and
carries them ahead of the manifest.** Criterion 5 asserts the mapping against
the manifest by reading it. Item C's own pull request extends `blog-idea`
from four targets to seven -- `researching`, `review` and `ready` -- and the
two pull requests land in different repositories, so a table that named only
today's four would break the moment C merged and before anything here
changed. It names all seven now. A target in this table that the manifest
does not have is not a failure of the assertion, which runs the other way:
every manifest target must be here.

`tip` is in the table and is **not** migrated. The manifest declares three
kinds and the criterion reads the manifest, so the table answers for three;
requirement 2 imports two of them, Blog Ideas and Topics, and requirement 3
says why not the third -- Obsidian stays a knowledge base for tips,
documentation and reference notes, and the pack reads no state from it.
Its six rows are here for the assertion, not for an importer: nothing walks
a tips directory.

A fourth kind, `skill-proposal`, was here until item A retired it from the
manifest. The vault's Skill Proposals are history, and its four rows left
this table with the kind, because a ladder word no kind declares is a rung
no note can be standing on.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..writes import promoted_rows, upsert_item
from . import Counts, Frozen, MigrationRefused, Record, digest
from .frontmatter import read as read_frontmatter

#: `source` on every row this migration writes.
SOURCE = "vault"

WHO = "vault migration"

#: The two bases requirement 2 migrates, as `(manifest kind, relative path)`.
#: The paths are the manifest's own `store.bases`, repeated here because the
#: manifest lives in another repository and this library installs without it.
BASES = (
    ("blog-idea", "System/Databases/Blog Ideas"),
    ("topic", "System/Databases/Topics"),
)

#: Where the vault is. `$OBSIDIAN_VAULT`, else a default under `~/Documents`.
DEFAULT_VAULT = "~/Documents/Obsidian Vault"

#: Where the writing manifest is, for the test that asserts this table covers
#: it. Nothing at runtime reads the manifest: the library installs into two
#: virtualenvs and the manifest is a third repository's file.
MANIFEST_RELATIVE = "writing-pack/sd-plugin.json"

#: `(manifest kind, ladder word) -> item status`. The one place a ladder word
#: becomes a status. Every word a kind can hold, including its initial one,
#: which is not a transition target and is where most notes actually sit.
STAGES: dict[tuple[str, str], str] = {
    # blog-idea: four targets in the manifest today, seven after item C's own
    # pull request adds `researching`, `review` and `ready`.
    ("blog-idea", "inbox"): "planning",
    ("blog-idea", "accepted"): "ready",
    ("blog-idea", "researching"): "in_progress",
    ("blog-idea", "drafting"): "in_progress",
    ("blog-idea", "review"): "in_progress",
    ("blog-idea", "ready"): "ready_to_send",
    ("blog-idea", "published"): "done",
    # Declined is finished with, not waiting on anything: `blocked` would put
    # it in front of the operator every sweep for a decision already made.
    ("blog-idea", "declined"): "done",
    ("topic", "candidate"): "planning",
    ("topic", "active"): "in_progress",
    ("topic", "parked"): "blocked",
    ("topic", "retired"): "done",
    ("tip", "inbox"): "planning",
    ("tip", "accepted"): "ready",
    ("tip", "ready"): "ready_to_send",
    ("tip", "approved"): "ready_to_send",
    ("tip", "published"): "done",
    ("tip", "declined"): "done",
}


def vault_root(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(os.path.expanduser(env.get("OBSIDIAN_VAULT") or DEFAULT_VAULT))


def manifest_path(environ: dict[str, str] | None = None) -> Path:
    """The writing pack's manifest, for the test that reads it."""
    env = os.environ if environ is None else environ
    named = env.get("SD_WRITING_MANIFEST")
    if named:
        return Path(os.path.expanduser(named))
    root = Path(os.path.expanduser(env.get("SD_REPO_ROOT") or "~/repos"))
    return root / MANIFEST_RELATIVE


def manifest_targets(path: Path | str) -> dict[str, set[str]]:
    """`kind -> every status word the manifest can put a note in`.

    Transition targets plus the initial status, because a note sitting in its
    initial status is the ordinary case and a table that mapped only the
    targets would refuse the largest group in the base.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for kind, body in (payload.get("kinds") or {}).items():
        words = {
            target
            for targets in (body.get("transitions") or {}).values()
            for target in targets
        }
        words |= set((body.get("transitions") or {}).keys())
        initial = body.get("initial-status")
        if initial:
            words.add(initial)
        found[kind] = words
    return found


@dataclass
class Reader:
    """The vault migration. Constructed with the root it reads."""

    root: Path
    bases: tuple[tuple[str, str], ...] = BASES
    name: str = SOURCE
    notes: list[str] = field(default_factory=list)

    @classmethod
    def at(cls, root: Path | str | None = None, **kwargs) -> "Reader":
        return cls(root=Path(root) if root is not None else vault_root(), **kwargs)

    # ------------------------------------------------------------ freeze

    def freeze(self) -> Frozen:
        records: dict[str, Record] = {}
        notes: list[str] = []
        for kind, relative in self.bases:
            base = self.root / relative
            if not base.is_dir():
                raise MigrationRefused(
                    f"{base} is not a directory; the vault's {kind} base is where "
                    f"the manifest's `store.bases` says it is, and an absent one "
                    f"would import as zero notes"
                )
            found = sorted(base.rglob("*.md"))
            notes.append(f"{SOURCE}: {len(found)} {kind} note(s) under {relative}")
            for path in found:
                matter, body = read_frontmatter(path.read_text(encoding="utf-8"))
                stage = str(matter.get("status") or "").strip()
                if not stage:
                    raise MigrationRefused(
                        f"{path} has no `status:` in its frontmatter; the ladder "
                        f"word is what `stage` keeps and there is nothing to keep"
                    )
                if (kind, stage) not in STAGES:
                    raise MigrationRefused(
                        f"{path} is at stage {stage!r}, which the mapping table "
                        f"does not name for {kind}. Add it to `STAGES` in this "
                        f"file, in review, before the import writes a status it "
                        f"guessed."
                    )
                identity = f"{kind}:{path.relative_to(self.root).as_posix()}"
                fields = {"kind": kind, **{str(key): value for key, value in matter.items()}}
                records[identity] = Record(
                    identity=identity,
                    payload={
                        "title": str(matter.get("title") or path.stem).strip(),
                        "stage": stage,
                        "status": STAGES[(kind, stage)],
                        "created": str(matter.get("dateCreated") or "").strip(),
                        "fields": digest(fields),
                        "body": digest({"markdown": body}),
                    },
                )
        return Frozen(source=self.name, records=records, notes=notes)

    # ------------------------------------------------------------ import

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        counts = Counts()
        for identity in sorted(frozen.records):
            payload = frozen.records[identity].payload
            kind, _, relative = identity.partition(":")
            path = self.root / relative
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
                    "fields": digest(json.loads(row["fields"] or "{}")),
                    "body": digest(json.loads(row["body"] or "{}")),
                },
            )
        return held


def stages_in(records: dict[str, Record]) -> set[str]:
    """Every ladder word a set of records holds. Criterion 5's set comparison."""
    return {str(record.payload.get("stage")) for record in records.values()}
