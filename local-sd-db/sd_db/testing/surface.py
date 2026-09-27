"""The connection surface: what the library touches on a connection.

A remote connection (`sd_db.remote`) can carry only the attributes of
`sqlite3.Connection` and `sqlite3.Cursor` that it implements. This module
names that set, and it proves the set is enough: `run` executes the
library's whole test suite with every `sd_db.connect` returning a guarded
connection that raises on any attribute outside it.

    python -m sd_db.testing.surface [--record FILE] [unittest args...]

`--record FILE` writes every attribute touched, with its count, as JSON; that
is how the sets below were measured. A test that fails under the guard is a
path the remote connection cannot carry.
"""

from __future__ import annotations

import atexit
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

#: What the suite touches on a connection `connect` returned, measured by
#: `--record` over the whole suite (library and tests together; the design
#: log has the counts). `__enter__`/`__exit__` are `with connection:`;
#: `row_factory=` is an assignment, the rest are reads. `total_changes`,
#: `iterdump`, `set_trace_callback` and `commit` are the tests' own probes;
#: `backup` is restore's copy of a candidate and the tests' snapshot. One
#: attribute the record found is deliberately absent: `create_function`,
#: which only `migrate` uses, on the `create=True` open this guard leaves
#: alone (`init` and `migrate` are hub verbs, and a session never creates).
CONNECTION_SURFACE = frozenset({
    "execute", "executescript", "close", "in_transaction", "row_factory=",
    "commit", "total_changes", "iterdump", "set_trace_callback", "backup",
    "__enter__", "__exit__",
})

#: What the suite touches on a cursor. A `hasattr(cursor, "keys")` probe
#: reads as absent here as it does on `sqlite3.Cursor`, so `keys` stays out.
CURSOR_SURFACE = frozenset({
    "fetchone", "fetchall", "lastrowid", "rowcount", "__iter__", "__next__",
})


class SurfaceViolation(AttributeError):
    """An attribute outside the named surface was touched."""


_touched: Counter[str] = Counter()
_enforce = True


def _touch(kind: str, name: str, surface: frozenset[str]) -> None:
    _touched[f"{kind}.{name}"] += 1
    if _enforce and name not in surface:
        raise SurfaceViolation(f"{kind}.{name} is outside the remote connection's surface")


class GuardedCursor:
    __slots__ = ("_cursor", "_connection")

    def __init__(self, cursor: sqlite3.Cursor, connection: GuardedConnection) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name: str):
        _touch("cursor", name, CURSOR_SURFACE)
        if name == "connection":
            return self._connection
        value = getattr(self._cursor, name)
        if name in ("execute", "executemany"):
            def call(*args, **kwargs):
                value(*args, **kwargs)
                return self
            return call
        return value

    def __setattr__(self, name: str, value) -> None:
        _touch("cursor", f"{name}=", CURSOR_SURFACE)
        setattr(self._cursor, name, value)

    def __iter__(self):
        _touch("cursor", "__iter__", CURSOR_SURFACE)
        return self

    def __next__(self):
        _touch("cursor", "__next__", CURSOR_SURFACE)
        return next(self._cursor)


class GuardedConnection:
    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name: str):
        _touch("connection", name, CONNECTION_SURFACE)
        value = getattr(self._connection, name)
        if name in ("execute", "executemany", "executescript", "cursor"):
            def call(*args, **kwargs):
                return GuardedCursor(value(*args, **kwargs), self)
            return call
        return value

    def __setattr__(self, name: str, value) -> None:
        _touch("connection", f"{name}=", CONNECTION_SURFACE)
        setattr(self._connection, name, value)

    def __enter__(self):
        _touch("connection", "__enter__", CONNECTION_SURFACE)
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        _touch("connection", "__exit__", CONNECTION_SURFACE)
        return self._connection.__exit__(*exc)


_previous: list = []


def install(*, enforce: bool = True) -> None:
    """Make every `sd_db.connect` in this process return a guarded connection.

    `uninstall` puts back whatever opener was there, so a test that guards
    inside a run over the wire leaves the wire in place for the next test.
    """
    global _enforce
    from .. import database

    _enforce = enforce
    _previous.append(database._opener)

    def opener(target, *, write, create, busy_timeout):
        opened = database.open_local(target, write=write, create=create, busy_timeout=busy_timeout)
        # `create=True` is `init` and `migrate`: hub verbs that never run
        # over the wire, so their connection is not held to the surface.
        return opened if create else GuardedConnection(opened)

    database._opener = opener


def uninstall() -> None:
    """Undo the latest `install`; with none left to undo, change nothing.

    An extra call must not clear an opener this module did not install,
    such as the wire opener of `sd-db.sh test --remote`.
    """
    from .. import database

    if _previous:
        database._opener = _previous.pop()


def touched() -> dict[str, int]:
    return dict(sorted(_touched.items()))


def main(argv: list[str]) -> int:
    import unittest

    record = None
    if argv[:1] == ["--record"]:
        record, argv = Path(argv[1]), argv[2:]
    install(enforce=record is None)
    if record is not None:
        atexit.register(lambda: record.write_text(json.dumps(touched(), indent=1) + "\n"))
    here = Path(__file__).resolve().parents[2]
    program = unittest.main(
        module=None,
        argv=["sd_db.testing.surface", "discover", "-s", str(here / "tests"), "-t", str(here), *argv],
        exit=False,
    )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
