"""The operator's open GitHub issues, as `shadow` rows and never as items.

Two of them on 2026-09-05, and the decision that they stay issues is the
whole point of this migration. One is in a shared benchmark repository, which
eight people can read: an issue closed with a pointer to a database only the
operator can reach would strand every other reader. So neither closes on
GitHub, both land in `shadow`, and `sd_db.shadow_sync` keeps their state.

**The repositories are read from the issues, not from a sentence.** The
requirement locates one of the two and not the other, and an earlier draft of
the plan placed the second in the pack as though it did. This asks GitHub
which repositories the operator's open issues are in, so the answer is
whatever is true on the day it runs. The operator's classic token carries
`repo` scope, which reaches the private ones.

**Issues, not pull requests.** `is:issue` is in the query rather than
filtered afterwards, because a filter applied after a paged walk is a filter
applied to whatever the page ceiling let through.

**`shadow` is shared**, so the verify answers only for the rows this source
froze -- a row the nightly sync adds afterwards is not a row GitHub lost.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..shadow_sync import available, normalize, search
from ..writes import upsert_shadow
from . import Counts, Frozen, MigrationRefused, Record, shadow_identity, shadow_tracker, shadow_url

SOURCE = "github-issues"

#: What "the two open GitHub issues" is, as a query. `author:@me` is the
#: operator's own, which is what the requirement means by "in the operator's
#: repositories": the two it counted are theirs.
QUERY = "author:@me is:issue is:open"


@dataclass
class Reader:
    """The GitHub issues migration. `runner` is the test's seam onto `gh`."""

    runner: object | None = None
    query: str = QUERY
    name: str = SOURCE
    frozen_identities: set[str] = field(default_factory=set)

    # ------------------------------------------------------------ freeze

    def freeze(self) -> Frozen:
        ok, reason = available(self.runner)
        if not ok:
            raise MigrationRefused(
                f"cannot read GitHub: {reason}. A migration that imported zero "
                f"issues because the credential was missing would report a clean "
                f"run over an empty source."
            )
        nodes, truncated, error = search(self.query, self.runner)
        if error:
            raise MigrationRefused(f"the search for {self.query!r} failed: {error}")
        if truncated:
            raise MigrationRefused(
                f"the search for {self.query!r} was cut short, so this freeze read "
                f"less than the source holds; a migration must not import a "
                f"partial answer"
            )
        records: dict[str, Record] = {}
        for node in nodes:
            row = normalize(node, "author")
            if row is None:
                continue
            # Keyed by `(tracker, url)`, as the table is: see `shadow_identity`.
            identity = shadow_identity(row["tracker"], row["url"])
            records[identity] = Record(
                identity=identity,
                payload={
                    "tracker": shadow_tracker(identity),
                    "repo": row["repo"] or None,
                    "number": row["number"],
                    "kind": row["kind"],
                    "title": row["title"] or None,
                    "state": row["state"] or None,
                    "author": row["author"] or None,
                },
            )
        self.frozen_identities = set(records)
        repos = sorted({record.payload["repo"] or "?" for record in records.values()})
        return Frozen(
            source=self.name,
            records=records,
            notes=[
                f"{SOURCE}: {len(records)} open issue(s) in "
                f"{', '.join(repos) if repos else 'no repository'}"
            ],
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
