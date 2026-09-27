"""The pack's `index.sqlite` cache, as `shadow` rows.

One thousand one hundred and seventy-five rows on 2026-09-05, all of them in
the cache's `issue` table. The cache is the pack's own -- rebuildable, never
an input to anything, living under `XDG_CACHE_HOME` rather than beside the
handoff packets -- and this migration copies what it holds into the table
that replaces it.

**This is the one place in the library that opens a database it does not
own.** Requirement 2's grep asks that `sqlite3.connect` appear only inside
this library, and it does: this file is inside it. What the grep is actually
protecting is that nothing *outside* invents a second store, and reading a
source database that is about to be retired is the opposite of that.

**The import is not the whole obligation.** A one-time copy would leave
`shadow` frozen on the day of the switch, because the collector that
refreshes those rows lives in the pack. `sd_db.shadow_sync` is that
collector, moved here in the same pull request, and PR 6's retire of the
cache is gated on a sync *after* this import having brought in a new issue.

**`shadow` is a shared table**, written by this migration and by every later
sync. So the verify answers only for the rows this source froze: a row the
sync added afterwards is not a row the cache lost.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..writes import upsert_shadow
from . import Counts, Frozen, MigrationRefused, Record, shadow_identity, shadow_tracker, shadow_url

#: `~/.cache/sd-ai-command-pack/index.sqlite`, honouring `XDG_CACHE_HOME`.
CACHE_RELATIVE = Path("sd-ai-command-pack/index.sqlite")

SOURCE = "index.sqlite"


def cache_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_CACHE_HOME") or ""
    home = Path(base) if base else Path(os.path.expanduser("~")) / ".cache"
    return home / CACHE_RELATIVE


@dataclass
class Reader:
    """The `index.sqlite` migration. Constructed with the cache to read."""

    path: Path
    name: str = SOURCE
    frozen_identities: set[str] = field(default_factory=set)

    @classmethod
    def at(cls, path: Path | str | None = None) -> "Reader":
        return cls(path=Path(path) if path is not None else cache_path())

    # ------------------------------------------------------------ freeze

    def freeze(self) -> Frozen:
        if not self.path.exists():
            raise MigrationRefused(
                f"no cache at {self.path}; the pack's dashboard builds it, and "
                f"an absent one would import as zero rows rather than as an error"
            )
        # Read-only, and with no pragmas: this database belongs to the pack
        # and this migration is a reader passing through.
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "issue" not in names:
                raise MigrationRefused(
                    f"{self.path} has no `issue` table; it holds {sorted(names)}. "
                    f"This is not the pack's index."
                )
            records: dict[str, Record] = {}
            for row in connection.execute("SELECT * FROM issue ORDER BY id"):
                url = (row["url"] or "").strip()
                if not url:
                    raise MigrationRefused(
                        f"{self.path} holds a row with no url ({row['id']!r}); "
                        f"`shadow` is keyed by (tracker, url) and half the key "
                        f"is missing"
                    )
                # Keyed by `(tracker, url)`, as the table is. The pack's cache
                # keys on both too, so it can hold one url under two trackers;
                # keying this dict by url alone would drop one of them here,
                # before anything downstream could notice.
                identity = shadow_identity(row["tracker"], url)
                if identity in records:
                    raise MigrationRefused(
                        f"{self.path} holds {url!r} twice under tracker "
                        f"{row['tracker']!r}; the source is not keyed as it claims"
                    )
                records[identity] = Record(
                    identity=identity,
                    payload={
                        "tracker": shadow_tracker(identity),
                        "repo": row["repo"] or None,
                        "number": row["number"],
                        "kind": row["kind"] or None,
                        "title": row["title"] or None,
                        "state": row["state"] or None,
                        "author": row["author"] or None,
                    },
                )
        finally:
            connection.close()
        self.frozen_identities = set(records)
        return Frozen(
            source=self.name,
            records=records,
            notes=[f"{SOURCE}: {len(records)} row(s) in {self.path}"],
        )

    # ------------------------------------------------------------ import

    def land(self, connection: sqlite3.Connection, frozen: Frozen) -> Counts:
        counts = Counts()
        held = self.rows(connection)
        for identity in sorted(frozen.records):
            record = frozen.records[identity]
            before = held.get(identity)
            upsert_shadow(connection, url=shadow_url(identity), **record.payload)
            if before is None:
                counts.record("inserted")
            elif before.hash == record.hash:
                counts.record("unchanged")
            else:
                counts.record("updated")
        return counts

    # ------------------------------------------------------------ verify

    def rows(self, connection: sqlite3.Connection) -> dict[str, Record]:
        """The `shadow` rows this source is answerable for, and no others.

        Restricted to what the freeze read: the table is shared with every
        later sync, and a row the sync added is not a row the cache lost.
        Before the freeze there is nothing to restrict to, so an unfrozen
        reader answers with an empty set rather than with the whole table.
        """
        held: dict[str, Record] = {}
        if not self.frozen_identities:
            return held
        for row in connection.execute("SELECT * FROM shadow ORDER BY tracker, url"):
            identity = shadow_identity(row["tracker"], row["url"])
            if identity not in self.frozen_identities:
                continue
            held[identity] = Record(
                identity=identity,
                payload={
                    "tracker": row["tracker"],
                    "repo": row["repo"],
                    "number": row["number"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "state": row["state"],
                    "author": row["author"],
                },
            )
        return held
