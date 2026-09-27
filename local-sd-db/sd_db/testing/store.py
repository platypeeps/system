"""A real store to point a test at, and the two shapes `restore` refuses.

Nothing outside this library opens the database -- `tests/test_one_store.py`
greps the whole repository for it -- and a fixture that *is* a database opens
one by definition. So the fixtures live here, in the part of the library that
other suites import, rather than in each suite that needs one.

`system`'s off-machine verifier is the caller this was written for. It hands a
snapshot to `restore`'s own validator, so its fixtures have to be real stores:
a toy two-table database is refused for its schema alone, and every failure
test below it would then pass for the wrong reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import paths
from ..database import set_schema_version
from ..schema import SCHEMA_VERSION, migrations

#: One fixed moment. A fixture whose bytes change between runs is a fixture
#: whose manifest hashes change between runs.
WHEN = "2026-09-21T00:00:00+00:00"

#: The tables the fixtures carry rows in. A manifest built from a store this
#: module wrote names these and nothing else.
FILLED = ("item", "note")


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        name: int(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
        for name in FILLED
    }


def make_store(path: Path | str, *, version: int | None = None, rows: int = 3) -> dict[str, int]:
    """Build a store at `path` and return the row counts it holds.

    `version` builds the shape that version had, by applying the migrations up
    to it and stamping the pragma. That is what a snapshot taken before the
    last migration looks like, and it is most of what a backup share holds, so
    a caller that can only build today's shape cannot test the case that
    matters on the day after a migration.

    The rows are items and the notes that point at them: a store with no rows
    proves nothing about counts, and a note whose item is missing is the
    refusal `break_foreign_keys` builds deliberately.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    version = SCHEMA_VERSION if version is None else version
    connection = sqlite3.connect(path)
    # 014 calls the conversion functions `migrate` registers (sd:1439).
    paths.install(connection)
    try:
        with connection:
            for number, migration in migrations():
                if number <= version:
                    connection.executescript(migration.read_text(encoding="utf-8"))
            set_schema_version(connection, version)
            for number in range(1, rows + 1):
                connection.execute(
                    "INSERT INTO item (id, kind, title, status, created_at,"
                    " updated_at, gate_generation) VALUES (?, 'task', ?,"
                    " 'planning', ?, ?, 0)",
                    (number, f"item {number}", WHEN, WHEN),
                )
                connection.execute(
                    "INSERT INTO note (item, timestamp, kind, body)"
                    " VALUES (?, ?, 'comment', ?)",
                    (number, WHEN, f"note {number}"),
                )
        return _counts(connection)
    finally:
        connection.close()


def break_foreign_keys(path: Path | str) -> dict[str, int]:
    """Add a note pointing at an item that is not there, and return the new counts.

    `restore` refuses this store: a copy holding orphaned rows is forensic
    evidence, not a restore point. The counts come back because the caller's
    manifest has to agree with them -- a fixture for the foreign-key refusal
    that also drifts the counts would fail for the other reason.
    """
    path = Path(path)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO note (item, timestamp, kind, body)"
                " VALUES (404, ?, 'comment', 'orphan')",
                (WHEN,),
            )
        return _counts(connection)
    finally:
        connection.close()


def add_unknown_table(path: Path | str, name: str = "stowaway") -> None:
    """Add a table no migration created, which `restore` refuses the store for.

    The finding this exists for: hashes, row counts, `integrity_check` and
    `foreign_key_check` all pass on a store whose table set `restore` will not
    accept. Only the committed validator sees it, so a verifier that keeps
    checks of its own reports such a store as a good backup.
    """
    if not name.isidentifier():
        raise ValueError(f"{name!r} is not a table name")
    path = Path(path)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
    finally:
        connection.close()
