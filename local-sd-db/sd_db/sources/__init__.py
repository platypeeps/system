"""The five migrations, and the fixed order they run in.

Requirement 2 fills the database from five sources. Each is one command, one
source, idempotent, reporting counts, in the order **freeze, import, verify,
retire**. This package holds all four now. It held the first three until the
`docs/work` retire landed, and the other four sources still have no retire
step of their own -- which is criterion 23's rule rather than a shortcut: the
retire step of a source runs in a pull request *after* the one that lands its
writer, so that the window in which the source is frozen is minutes rather
than a slice.

    frozen = source.freeze()           # read the source once, at one moment
    counts = land(connection, source, frozen)
    differences = verify(connection, source, frozen)
    retired = source.retire(connection, frozen)   # once, and never again

**The retire is a sitting, not a fourth line in a script.** `retire()` below
runs freeze, import and verify once more, refuses on anything they find,
takes a snapshot, and only then lets the source stop being the source. The
refusals come first because a refusal that has already written something is
not a refusal; the snapshot comes before the one irreversible step; and the
row is switched before the commit, because a source whose lines are gone
while the rows still say `file` is a source nobody can read, where the other
order leaves only a line that is stale.

**The freeze is a refusal, not a row.** A sitting freezes its source by
refusing to run while anything is still writing it -- an uncommitted `prd.md`,
a vault file open in Obsidian's own writer -- and it lifts the freeze by
returning, leaving the source exactly as it found it. There is no `freeze`
kind in `state` because there is nothing to remember: a process that died
mid-sitting left a source that was never modified. What *is* remembered is
the verify, as a `verified` row carrying the source, the repository and the
content hash it found equal, which is what a later retire is allowed to
trust. A verify that finds a difference writes no such row, which is exactly
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
from pathlib import Path
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

    def record(self, what: str) -> None:
        self.seen += 1
        setattr(self, what, getattr(self, what) + 1)

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
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
    """What a migration is. Five of these, and no base class between them.

    The sources share this shape and nothing else: one reads SQLite, one
    reads git trees, one reads a markdown register, one reads a vault, one
    reads GitHub. A base class would have to abstract over that and would buy
    nothing, since the contract is three methods and an optional fourth.
    """

    name: str

    def freeze(self) -> Frozen:
        """Read the source once, refusing if anything is still writing it."""

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        """Write the frozen records as rows. Idempotent."""

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        """What the database holds, in the source's own shape, for the verify."""

    def retire(self, connection: sqlite3.Connection, frozen: Frozen) -> "Retired":
        """Stop being the source. Once, in one commit, after the verify.

        Optional, and deliberately so: four of the five sources have no
        retire step built yet, and `retire()` below asks whether a source
        carries this method rather than assuming every source does. A
        `Protocol` with a body is not enforcement -- the enumeration in
        `retire()` is.
        """


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
    # An idea promoted to a writing piece left the source's rows; the piece
    # owns it now and keeps the identity in `promoted_from` (sd:1994).
    promoted = {row[0] for row in connection.execute(
        "SELECT json_extract(fields, '$.promoted_from.external_id') FROM item WHERE "
        "source = 'writing-piece' AND piece IS NOT NULL AND CASE WHEN json_valid(fields) THEN json_extract(fields, '$.promoted_from.source') = ? END",
        (source.name,))}
    differences: list[Difference] = []
    for identity in sorted(set(frozen.records) - set(held) - promoted):
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
    #: What the freeze read, kept so a retire does not freeze the source a
    #: second time. Two freezes are two moments, and the second one would be
    #: reading a source the first one has already been trusted about.
    frozen: Frozen | None = None

    @property
    def clean(self) -> bool:
        return not self.differences

    def report(self) -> list[str]:
        lines = [
            f"{self.source}: {self.counts.seen} seen, "
            f"{self.counts.inserted} inserted, {self.counts.updated} updated, "
            f"{self.counts.unchanged} unchanged"
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
    """Freeze, import, verify. Retires nothing; `retire()` below is that step.

    A verify difference writes no `verified` row, which is what lifts the
    freeze: `retire()` reads that row and refuses without one carrying this
    run's frozen hash. Nothing about the source is touched either way -- a
    source stays authoritative and stays written by whatever writes it today
    until its own retire lands, which for four of the five is still every
    time this runs.
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


# ----------------------------------------------------------------- the retire


@dataclass
class Retired:
    """What one source's own retire step did to the source itself.

    `removed` and `kept` are counted separately and both are reported. The
    archive is not an oversight the retire missed: an archived item's line is
    a record of what its status *was*, and a retire that swept the whole tree
    would rewrite history to say the database knows a status for an item
    nobody will ever ask about again.
    """

    removed: int = 0
    kept: int = 0
    commits: dict[str, str] = field(default_factory=dict)
    switched: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Retirement:
    """One retire sitting, end to end, and what each phase did."""

    source: str
    pack: str
    sitting: Sitting | None
    snapshot: Path | None
    retired: Retired

    def report(self) -> list[str]:
        lines = [f"{self.source}: retiring under pack {self.pack}"]
        if self.sitting is not None:
            lines.extend(self.sitting.report())
        if self.snapshot is not None:
            lines.append(f"{self.source}: snapshot at {self.snapshot}")
        lines.extend(self.retired.notes)
        for path in self.retired.switched:
            lines.append(f"{self.source}: {path} now reads status from the row")
        for path, commit in sorted(self.retired.commits.items()):
            lines.append(f"{self.source}: {path} retired in {commit[:12]}")
        for path in self.retired.already:
            lines.append(f"{self.source}: {path} was already retired; left alone")
        lines.append(
            f"{self.source}: {self.retired.removed} line(s) removed, "
            f"{self.retired.kept} archived line(s) kept"
        )
        return lines


def verified_for(
    connection: sqlite3.Connection, source: str, frozen_hash: str
) -> int | None:
    """The `verified` row that agrees with this hash, newest first, or `None`.

    Read back from the table rather than taken from the `Sitting` the caller
    is holding, because the row is the thing the retire is allowed to trust
    and an object in memory is not it. The body is JSON written by
    `record_state`; a row whose body will not parse is not a row that agrees.
    """
    for row in connection.execute(
        "SELECT id, body FROM state WHERE kind = 'verified' AND key = ? "
        "ORDER BY id DESC",
        (source,),
    ):
        try:
            body = json.loads(row["body"] or "{}")
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("hash") == frozen_hash:
            return int(row["id"])
    return None


def retire(
    connection: sqlite3.Connection,
    source: Source,
    *,
    token: str,
    home,
    environ: dict[str, str] | None = None,
    snapshot: bool = True,
) -> Retirement:
    """One sitting: refuse, import, verify, snapshot, switch the row, commit.

    The order is the whole design, and every step of it was put there by a
    way this can go wrong.

    1. **The pack first.** The retire hands a question to the database, and
       every reader of that question has to be able to ask the new way before
       the old way stops existing. Checked before anything is read, so the
       refusal costs nothing and leaves nothing behind.
    2. **A source with no retire step stops here**, which is four of the five
       and says so rather than failing on a missing attribute. A source that
       has already been retired stops here too, and reports it: a sitting run
       twice answers "there was nothing left" rather than freezing an empty
       source and calling every row that exists a difference.
    3. **A `verified` row must already exist for this source.** A source that
       has never once agreed with the rows is a source nobody has imported;
       this refusal comes before the import so that the operator is told to
       run the import rather than having one run silently underneath them.
    4. **Freeze, import, verify once more.** The freeze refuses on an
       uncommitted file, naming it. The import carries a line the old command
       moved since the last sitting into the row, which is the entire reason
       the sitting imports again rather than trusting yesterday's rows.
    5. **A `verified` row written by *this* sitting for the hash it just
       froze.** A difference the import could not settle writes no such row,
       and the retire stops. The lines are all still in place at that point,
       which is what "a verify difference lifts the freeze" means from the
       source's side. It has to be this sitting's row and not merely a
       matching one: a source that has not changed since yesterday has
       yesterday's row with today's hash on it, and the rows may have drifted
       in between.
    6. **The snapshot**, through the ordinary backup path, so the thing that
       proves it is the same thing that proves every other backup. It is the
       last step before the only irreversible one.
    7. **The source's own retire**, which switches the row and then commits.

    Steps 1 to 5 leave the source untouched. Steps 1 to 4's freeze also
    leave the rows untouched; step 5's refusal comes after an import, so the
    rows then hold what the source says, which is where an import always
    leaves them and is the state the next sitting starts from anyway.
    """
    # Imported here and not at the top: `sd_db/__init__` imports `backup`
    # before `sources`, and a module-level import of it from this side is a
    # cycle. `pack` follows it down for no reason but to sit beside it. The
    # name is reached through the module and not through the package, because
    # `sd_db.backup` the attribute is `backup.run` re-exported.
    from ..backup import run as take_snapshot
    from ..pack import installed

    found = installed(home=home, environ=environ)
    if not found.usable:
        raise MigrationRefused(found.refusal())

    if not hasattr(source, "retire"):
        raise MigrationRefused(
            f"{source.name} has no retire step built; it stays authoritative "
            f"and stays written by whatever writes it today"
        )

    # A source that knows part of what it reads is already retired hands back
    # a reader narrowed to the rest; one that does not is read whole. Asked
    # of the source rather than switched on its name, so this function holds
    # no list of sources to keep in step with anything.
    narrow = getattr(source, "for_retire", None)
    if narrow is not None:
        source = narrow()
    nothing_left = getattr(source, "nothing_left", None)
    if nothing_left is not None and nothing_left():
        return Retirement(
            source=source.name,
            pack=found.version,
            sitting=None,
            snapshot=None,
            retired=source.retire(connection, None),
        )

    if not list(
        connection.execute(
            "SELECT id FROM state WHERE kind = 'verified' AND key = ? LIMIT 1",
            (source.name,),
        )
    ):
        raise MigrationRefused(
            f"{source.name} has never been verified against the rows, so there "
            f"is no `verified` row to trust; run `sd-db.sh import "
            f"{token}` and read what it says before retiring anything"
        )

    sitting = run(connection, source)
    # The row this sitting's own verify wrote, read back from the table
    # rather than taken from the object in hand -- and it must be *this*
    # sitting's. An earlier sitting's row can carry the same hash while the
    # rows have drifted since, and a gate that accepted it would retire the
    # source on the strength of a verify that ran yesterday.
    if sitting.verified is None or (
        verified_for(connection, source.name, sitting.frozen_hash) != sitting.verified
    ):
        raise MigrationRefused(
            f"{source.name}: this sitting wrote no `verified` row for source "
            f"hash {sitting.frozen_hash[:12]}, so the rows and the source do "
            f"not agree and nothing may be retired. "
            + "; ".join(str(one) for one in sitting.differences)
        )

    taken = take_snapshot(home=home).directory if snapshot else None
    return Retirement(
        source=source.name,
        pack=found.version,
        sitting=sitting,
        snapshot=taken,
        retired=source.retire(connection, sitting.frozen),
    )
