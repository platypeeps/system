"""The migrations, and the fixed order they run in.

Requirement 2 filled the database from five sources. Four remain; the
`docs/work` importer and its retire are gone, since every item's status
lives in its row (sd:3231). Each is one command, one source, idempotent,
reporting counts, in the order **freeze, import, verify**.

    frozen = source.freeze()           # read the source once, at one moment
    counts = land(connection, source, frozen)
    differences = verify(connection, source, frozen)

**The freeze is a refusal, not a row.** A sitting freezes its source by
refusing to run while anything is still writing it -- an uncommitted `prd.md`,
a vault file open in Obsidian's own writer -- and it lifts the freeze by
returning, leaving the source exactly as it found it. There is no `freeze`
kind in `state` because there is nothing to remember: a process that died
mid-sitting left a source that was never modified. What *is* remembered is
the verify, as a `verified` row carrying the source, the repository and the
content hash it found equal. A verify that finds a difference writes no such row, which is exactly
how "a seeded verify difference lifts the freeze with the source unchanged"
reads from the database afterwards.

**Verify compares identity and content, never counts.** Two sets of the same
size are not the same set, and a migration that checked the count would agree
with itself while a renamed file landed twice. Every difference is named, on
the record, with what the source says and what the row says.

**Idempotency is the property the second run proves.** `land` returns
inserted, updated and unchanged separately, so criterion 5's "runs twice and
reports the same counts the second time with zero new rows" is a statement
about the returned object rather than about a log line somebody read.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from ..errors import SdDbError
from ..writes import now, record_state


class MigrationRefused(SdDbError):
    """A source that cannot be read, or a sitting that must not proceed."""


def digest(payload: dict) -> str:
    """One content hash, over the canonical form of what the source holds.

    Sorted keys and a fixed separator, so a dictionary that came back from
    the database in another order hashes the same as the one read from the
    file. A hash that depended on iteration order would report a difference
    on every second run.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Record:
    """One thing a source holds: its identity in the source, and its content."""

    identity: str
    payload: dict

    @property
    def hash(self) -> str:
        return digest(self.payload)


def shadow_identity(tracker: str | None, url: str) -> str:
    """A `shadow` row's identity, for the two sources that write that table.

    `shadow` is keyed by `(tracker, url)` (sd:603), so a source keyed by url
    alone collapses two trackers' rows into one on the way in -- silently,
    which is the same defect the url-alone unique index had, moved from the
    store to the importer. A tracker name holds no space, so one separates
    the two halves and keeps a `Difference` line readable; a tracker that
    does hold one is refused rather than split in the wrong place.
    """
    tracker = (tracker or "").strip()
    if not tracker or " " in tracker:
        raise MigrationRefused(
            f"a shadow row's tracker must be one word and not empty; got "
            f"{tracker!r} for {url!r}"
        )
    return f"{tracker} {url}"


def shadow_url(identity: str) -> str:
    """The url half of a `shadow_identity`."""
    return identity.partition(" ")[2]


def shadow_tracker(identity: str) -> str:
    """The tracker half of a `shadow_identity`, trimmed as it was keyed.

    Stored in place of the raw column, so the row lands under the tracker
    the freeze keyed it by and a later sync's row for the same url is the
    same row rather than a second one.
    """
    return identity.partition(" ")[0]


@dataclass
class Frozen:
    """A source read once, at one moment, with what it took to read it.

    `commits` carries the commit each record was read from where the source
    is a git tree, and is empty where it is not. `notes` carries what the
    reader wants the sitting's report to say -- a branch a record came from,
    a line the source states in prose -- without inventing a column for it.
    """

    source: str
    records: dict[str, Record]
    commits: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        """The whole source as one hash, which is what a `verified` row holds."""
        return digest({identity: record.hash for identity, record in self.records.items()})


@dataclass
class Counts:
    """What one import did. Compared between runs, not read as a total."""

    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    #: Records of an idea promoted to a writing piece that changed since; reported, not written.
    promoted_changed: int = 0

    def record(self, what: str) -> None:
        self.seen += 1
        setattr(self, what, getattr(self, what) + 1)

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "promoted_changed": self.promoted_changed,
        }


@dataclass(frozen=True)
class Difference:
    """One thing the source and the rows disagree about, named."""

    identity: str
    what: str
    in_source: object
    in_rows: object

    def __str__(self) -> str:
        return f"{self.identity}: {self.what}: source {self.in_source!r}, rows {self.in_rows!r}"


class Source(Protocol):
    """What a migration is. Four of these, and no base class between them.

    The sources share this shape and nothing else: one reads SQLite, one
    reads a markdown register, one reads a vault, one reads GitHub. A base
    class would have to abstract over that and would buy nothing, since the
    contract is three methods.
    """

    name: str

    def freeze(self) -> Frozen:
        """Read the source once, refusing if anything is still writing it."""

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        """Write the frozen records as rows. Idempotent."""

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        """What the database holds, in the source's own shape, for the verify."""



def land(connection: sqlite3.Connection, source: Source, frozen: Frozen) -> Counts:
    """Import, which is the source's own method. Here so the order reads as one."""
    return source.land(connection, frozen)


def verify(
    connection: sqlite3.Connection, source: Source, frozen: Frozen
) -> list[Difference]:
    """Every way the frozen source and the rows disagree, by identity and content.

    Three kinds of difference, all named rather than counted: a record the
    rows do not have, a row the source does not have, and a pair whose
    content hashes differ. The third reports the fields that actually differ,
    because "the hashes differ" sends the operator to a diff they then have to
    construct themselves.
    """
    held = source.rows(connection)
    differences: list[Difference] = []
    for identity in sorted(set(frozen.records) - set(held)):
        differences.append(Difference(identity, "missing from the rows", "present", None))
    for identity in sorted(set(held) - set(frozen.records)):
        differences.append(Difference(identity, "no longer in the source", None, "present"))
    for identity in sorted(set(frozen.records) & set(held)):
        source_record = frozen.records[identity]
        row_record = held[identity]
        if source_record.hash == row_record.hash:
            continue
        keys = sorted(set(source_record.payload) | set(row_record.payload))
        for key in keys:
            in_source = source_record.payload.get(key)
            in_rows = row_record.payload.get(key)
            if in_source != in_rows:
                differences.append(Difference(identity, key, in_source, in_rows))
    return differences


@dataclass
class Sitting:
    """One migration's freeze, import and verify, and what came of it."""

    source: str
    counts: Counts
    differences: list[Difference]
    frozen_hash: str
    verified: int | None
    notes: list[str] = field(default_factory=list)
    #: What the freeze read, kept for the caller that reports on it.
    frozen: Frozen | None = None

    @property
    def clean(self) -> bool:
        return not self.differences

    def report(self) -> list[str]:
        lines = [
            f"{self.source}: {self.counts.seen} seen, "
            f"{self.counts.inserted} inserted, {self.counts.updated} updated, "
            f"{self.counts.unchanged} unchanged"
            + (f", {self.counts.promoted_changed} promoted and changed since, not written"
               if self.counts.promoted_changed else "")
        ]
        lines.extend(self.notes)
        if self.clean:
            lines.append(f"{self.source}: verified, source hash {self.frozen_hash[:12]}")
        else:
            lines.append(
                f"{self.source}: verify found {len(self.differences)} difference(s); "
                f"the freeze is lifted and the source is unchanged"
            )
            lines.extend(f"{self.source}:   {difference}" for difference in self.differences)
        return lines


def run(connection: sqlite3.Connection, source: Source) -> Sitting:
    """Freeze, import, verify.

    A verify difference writes no `verified` row, which is what lifts the
    freeze. Nothing about the source is touched either way: a source stays
    authoritative and stays written by whatever writes it today.
    """
    frozen = source.freeze()
    counts = land(connection, source, frozen)
    differences = verify(connection, source, frozen)
    verified = None
    if not differences:
        verified = record_state(
            connection,
            "verified",
            key=source.name,
            body={
                "source": source.name,
                "hash": frozen.hash,
                "records": len(frozen.records),
                "at": now(),
            },
        )
    return Sitting(
        source=source.name,
        counts=counts,
        differences=differences,
        frozen_hash=frozen.hash,
        verified=verified,
        notes=list(frozen.notes),
        frozen=frozen,
    )
