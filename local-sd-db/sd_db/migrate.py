"""Applying migrations, by one explicit command and never on open.

`init` and `migrate` are the same operation on a database that does not exist
yet, which is why they share this module. What they never share is a caller:
`init` is run once by hand, `migrate` is run by the operator with the
dashboard and the runner stopped, after a backup.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .database import connect, schema_version
from .schema import SCHEMA_VERSION, VERSION_PRAGMA, migrations

#: A migration file that opens its own transaction breaks the wrapping below.
BEGINS = re.compile(r"^\s*BEGIN\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class Applied:
    """What a migration run did, so the caller can report it and a test can
    assert the second run did nothing."""

    path: Path
    before: int
    after: int
    applied: list[int]

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def pending(connection: sqlite3.Connection) -> list[tuple[int, Path]]:
    found = schema_version(connection)
    return [(version, path) for version, path in migrations() if version > found]


def migrate(path: Path | str | None = None, *, home: Path | str | None = None) -> Applied:
    """Apply every migration the database has not seen, in order.

    Idempotent: a second run finds nothing pending and reports the same
    version with an empty list. Each file is applied inside its own
    transaction with the version bump in the same transaction, so a failure
    halfway leaves the database at the last version it fully reached rather
    than at a shape no number describes.
    """
    connection = connect(path, home=home, create=True, write=True)
    try:
        # 014 converts repository paths with these two functions, reading
        # `$HOME` in this process. They are registered here, on the one
        # connection that runs migrations, and nowhere a query runs.
        paths.install(connection)
        before = schema_version(connection)
        applied: list[int] = []
        for version, source in pending(connection):
            statements = source.read_text(encoding="utf-8")
            if BEGINS.search(statements):
                raise ValueError(
                    f"{source.name} opens its own transaction; migration files "
                    f"must not, because this function wraps them in one"
                )
            # `executescript` commits whatever is open before it runs, so an
            # outer transaction here would be closed under us and the COMMIT
            # would fail with "no transaction is active" -- observed on
            # 2026-09-06. The transaction goes inside the script instead, with
            # the version bump in it, so a failure halfway leaves the database
            # at the last version it fully reached.
            connection.executescript(
                f"BEGIN;\n{statements}\n"
                f"PRAGMA {VERSION_PRAGMA} = {int(version)};\nCOMMIT;"
            )
            applied.append(version)
        after = schema_version(connection)
        target = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        return Applied(path=target, before=before, after=after, applied=applied)
    finally:
        connection.close()


def initialise(path: Path | str | None = None, *, home: Path | str | None = None) -> Applied:
    """Create the database and bring it to the current version.

    Separate from `migrate` only in what it means to the operator: `init` on
    a machine that has none, `migrate` on one that does. The refusal to
    create silently lives in `connect`; both of these pass `create=True`
    deliberately.
    """
    result = migrate(path, home=home)
    if result.after != SCHEMA_VERSION:
        raise AssertionError(
            f"init left the database at {result.after}, expected {SCHEMA_VERSION}"
        )
    return result
