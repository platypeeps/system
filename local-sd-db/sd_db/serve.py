"""The hub half: serve one database to `sd_db.remote` connections.

    python -m sd_db.serve --loopback [--port N] [--database PATH]

Step 2 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`:
bound to 127.0.0.1 only. Each session is one TCP
connection and one hub-side connection from the library's own local open,
so a remote statement meets the same pragmas, the same version refusals and
the same SQLite write lock as a local one.

One server per file. The server takes `<database>.serve.lock` beside it
through `runner_journal.lock` before it listens, and a second server over the
same file exits non-zero naming the lock. Ownership of a request id (step 5)
lives in the server's memory, so two servers over one file would be two
registries.

Loopback is not a user boundary: every local account reaches 127.0.0.1.
The server writes a fresh token to `<database>.serve.token` beside it, mode
0600, and refuses an `open` frame without it. A client that sends it could
read the owner's file, so it runs as the owner or as root.

The `open` frame carries the client's build (step 3): its protocol version,
its package version and its `SCHEMA_VERSION`. The server refuses any
difference from its own with `sd_db.remote.BuildMismatch`, naming the side
to upgrade, before it opens the file.

A session may name the file it opens. That is what lets the library's
suite, which builds a database per test, run over the wire; behind the
token it grants nothing the owner's own processes lack. Step 7, which binds
the tailnet address, serves the hub's own database and nothing else to a
peer.

The server logs, per write transaction, the longest gap between two frames
inside it, and on exit the longest across the run (gap (g) of the design).

Step 5, the unknown outcome; the operator's ruling of 2026-10-04 builds it
without the prune, so no row is ever deleted:

- `BEGIN IMMEDIATE` carries a request id R. The session owns R from before
  the statement runs, and an id seen before is refused.
- `COMMIT` from the owner inserts the `request_outcome` row on the same
  connection, then commits: the row and the writes land together or not
  at all. Each attempt checks for the row inside the transaction: a COMMIT
  a deferred foreign key failed leaves the transaction open, and a
  `ROLLBACK TO` can remove the row it inserted. A transaction that wrote
  nothing gets no row: committed or not, it wrote nothing, and the table,
  which is never pruned, does not grow on quiet ticks. "Wrote" is
  measured two ways. `total_changes` counts every row an INSERT, UPDATE
  or DELETE changed, in any database, so a replay whose UPDATE matches
  nothing stays empty. Every other action that is not a read, in any
  database, counts as a write the moment the authorizer sees it
  prepared: DDL, a pragma that sets a value, ATTACH (the third review of
  2026-10-04). A row too many says only that the transaction committed,
  which it did. The server's row stays out of the session's
  `total_changes`. A `COMMIT` naming an R the session does not own is
  refused and not applied, and a write transaction's `COMMIT` without its
  R too.
- `ROLLBACK`, a transaction SQLite ends itself, and the socket closing
  settle ownership.
- `outcome(R)` answers `in_flight` while a session owns R, else `recorded`
  when the row exists, else `absent`. Admission, settlement and the answer
  run under one lock per id, so absence proves the transaction ended
  without committing: nothing unowned can commit.
- A plain `BEGIN` carries no R and opens a read transaction under
  `query_only` (gap C2). Its `COMMIT` or `ROLLBACK` needs no R and writes
  no row.
- SQLite's authorizer on the session's connection refuses BEGIN, COMMIT
  and ROLLBACK unless the hub routes them, whatever the text: a comment
  or a trailing statement cannot end a write transaction without its row
  (the review of 2026-10-04). A transaction a `SAVEPOINT` opens is a read
  under `query_only`. A script runs outside any transaction the hub
  holds, carries no R, and must end the transaction it opens. A client cannot set
  `query_only` inside a transaction the hub holds (the second review of
  2026-10-04). A VACUUM's own BEGIN and COMMIT pass: SQLite refuses a
  VACUUM inside a transaction. The client does
  not check this itself: a text `remote.statement` misses goes as a plain
  statement, and this refusal answers it.
- A session silent for `IDLE_TIMEOUT` inside an open transaction is closed,
  which rolls it back: below `database.BUSY_TIMEOUT`, so a hub writer
  waiting behind an abandoned satellite gets the lock (gap (g)).
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import secrets
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import database, remote
from .runner import RunnerRefused
from .runner_journal import lock
from .writes import now

LOOPBACK = "127.0.0.1"
#: Suffixes on the database's own file name: one lock and one token per
#: database, so two files in one folder are served side by side.
LOCK_SUFFIX = ".serve.lock"
TOKEN_SUFFIX = ".serve.token"
#: Seconds a session may stay silent inside an open transaction before the
#: server closes it, which rolls it back. Below `database.BUSY_TIMEOUT`
#: (5 s): a hub writer that starts waiting when a satellite goes silent gets
#: the lock before its own wait ends (gap (g) of the design).
IDLE_TIMEOUT = 4.0
#: What `Session._authorize` counts as a read, with `load_extension` left
#: out of `SQLITE_FUNCTION` and a `PRAGMA` read only with no argument.
#: SAVEPOINT, RELEASE and ROLLBACK TO move no data: the library's nested
#: `transaction()` uses them in a write that may change nothing.
READS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT})
#: Row writes, which `total_changes` counts in every database. A DROP TABLE
#: or the REPLACE half of an upsert changes rows it does not count, but the
#: first is `SQLITE_DROP_TABLE` and the second comes with its counted insert.
ROWS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
#: A statement that is a VACUUM: Python runs one statement per `execute`.
VACUUM = re.compile(r"\s*VACUUM\b", re.IGNORECASE)

#: A test seam, `None` everywhere else: called as `_fault(point, session)` at
#: `before-commit` (the `COMMIT` frame received, nothing recorded yet) and at
#: `after-commit` (committed, nothing answered yet). A module attribute and
#: not an environment variable, for `database._opener`'s reason.
_fault = None


class Owners:
    """Which session owns each request id, with one lock per id.

    In memory: a hub that restarts has rolled back every transaction it held
    open, so an empty registry is the truth after a restart. Ids admitted
    since the server started stay in `seen`, so an id is used once; a
    recorded id is refused from its row across restarts too.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owners: dict[str, int] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._seen: set[str] = set()

    def lock(self, rid: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(rid, threading.RLock())

    def admit(self, rid: str, session: int) -> bool:
        """Own `rid` for `session`, unless any session ever owned it."""
        with self._guard:
            if rid in self._seen:
                return False
            self._seen.add(rid)
            self._owners[rid] = session
            return True

    def owner(self, rid: str) -> int | None:
        with self._guard:
            return self._owners.get(rid)

    def settle(self, rid: str) -> None:
        with self._guard:
            self._owners.pop(rid, None)
            self._locks.pop(rid, None)


def _request_id(rid) -> str:
    if not isinstance(rid, str) or not remote.REQUEST_ID.fullmatch(rid):
        raise remote.RemoteError(f"refused: {rid!r} is not a request id (a ULID)")
    return rid


class Log:
    """One line per event on a stream, and the run's longest gap."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.guard = threading.Lock()
        self.longest_gap = 0.0
        self.longest_where = ""
        self.transactions = 0

    def line(self, text: str) -> None:
        with self.guard:
            self.stream.write(f"sd-db serve: {text}\n")
            self.stream.flush()

    def transaction(self, session: int, frames: int, gap: float, wall: float, after: str) -> None:
        """`after` is the frame that ended the longest gap: what the body was
        doing before it sent that is what a finding names."""
        with self.guard:
            self.transactions += 1
            if gap > self.longest_gap:
                self.longest_gap = gap
                self.longest_where = after
        self.line(
            f"session {session} write transaction: {frames} frames, "
            f"longest gap {gap * 1000:.1f} ms, wall {wall * 1000:.1f} ms, before {after}"
        )

    def summary(self) -> str:
        return (
            f"longest in-transaction frame gap {self.longest_gap * 1000:.1f} ms "
            f"across {self.transactions} write transactions, before {self.longest_where}"
        )


def _answer(connection: sqlite3.Connection, cursor: sqlite3.Cursor | None) -> dict:
    answer = {"ok": True, "in_transaction": connection.in_transaction}
    if cursor is not None:
        rows = cursor.fetchall()
        answer["description"] = (
            [column[0] for column in cursor.description] if cursor.description is not None else None
        )
        answer["rows"] = [[remote.encode_value(value) for value in row] for row in rows]
        answer["lastrowid"] = cursor.lastrowid
        answer["rowcount"] = cursor.rowcount
    return answer


def _brief(frame: dict) -> str:
    """A frame in one short, quoted line, for the log."""
    text = " ".join(str(frame.get("sql") or frame.get("op")).split())
    return repr(text[:100])


class Session(socketserver.BaseRequestHandler):
    """One remote connection, from its `open` frame to its socket closing."""

    counter = 0
    counter_guard = threading.Lock()

    def setup(self) -> None:
        #: What opened the open transaction: `write` (with `rid`), `read`,
        #: or `None` for none, or one opened some other way.
        self.kind: str | None = None
        self.rid: str | None = None
        #: `total_changes` when the open write transaction began.
        self.mark = 0
        #: The open write transaction prepared a write that moves no row.
        self.wrote = False
        #: Rows the server inserted, kept out of `total_changes`.
        self.hidden = 0
        #: The connection's own `query_only`, restored when a read ends.
        self.query_only = 0
        self.traced: list[str] = []
        #: The hub runs or routes the statement now, so `_authorize` passes it.
        self.routing = False
        #: Why `_authorize` refused the statement, for the error frame.
        self.denied: str | None = None

    def _authorize(self, action: int, arg1, arg2, database_name, trigger) -> int:
        """SQLite's authorizer on the session's connection.

        SQLite calls it for each statement it prepares, with what the
        statement does, so a transaction boundary or a write is seen
        whatever its text looks like: a comment, a trailing statement, any
        case, any database. One case per action code; later rules add
        theirs here. The session caches no statement (`open_local`'s
        `cached_statements=0`): Python does not authorize a cached one
        again, and a write it reused would leave `wrote` unset.
        """
        if not self.routing and action not in ROWS and not (
                action in READS and (arg2 or "").lower() != "load_extension"
                or action == sqlite3.SQLITE_PRAGMA and arg2 is None):
            # Anything not a read may write, in any database; `_commit`
            # counts row writes itself. The hub's own statements run under
            # `routing` and do not count.
            self.wrote = True
        if action == sqlite3.SQLITE_TRANSACTION:
            # BEGIN, COMMIT and ROLLBACK run only as `_statement` routes
            # them, so a write transaction owns its id and records its row.
            if self.routing:
                return sqlite3.SQLITE_OK
            return self._deny(f"{arg1} outside the hub's transaction handling; send BEGIN IMMEDIATE, "
                              f"BEGIN, COMMIT or ROLLBACK as the whole statement, with no comment")
        if action == sqlite3.SQLITE_PRAGMA:
            # SQLite passes the pragma's name as `arg1` and its value as
            # `arg2`, whatever the case, spacing or schema prefix. Inside a
            # read the hub holds, `query_only` keeps the read's COMMIT or
            # RELEASE from committing writes no id owns, so inside any
            # transaction the hub holds, only the hub sets it: its own
            # statements run in `_quiet`, under `routing`. Outside one it is
            # the session's, which a read saves and restores: a backup of a
            # database waiting for `migrate` switches it off on a `mode=ro`
            # open. Reading it is always allowed.
            if (arg1 or "").lower() == "query_only" and arg2 is not None \
                    and self.kind is not None and not self.routing:
                return self._deny("query_only is the hub's inside a transaction; "
                                  "set it before BEGIN")
        return sqlite3.SQLITE_OK

    def _deny(self, reason: str) -> int:
        self.denied = f"refused: {reason}"
        return sqlite3.SQLITE_DENY

    @contextmanager
    def _routed(self):
        """A transaction statement the hub runs on purpose."""
        self.routing = True
        try:
            yield
        finally:
            self.routing = False

    def _quiet(self, connection: sqlite3.Connection, sql: str, params=()) -> sqlite3.Cursor:
        """A statement of the server's own, kept out of the session's trace."""
        mark = len(self.traced)
        try:
            with self._routed():
                return connection.execute(sql, params)
        finally:
            del self.traced[mark:]

    def _recorded(self, connection: sqlite3.Connection, rid: str) -> bool:
        return self._quiet(connection, "SELECT 1 FROM request_outcome WHERE id = ?", (rid,)).fetchone() is not None

    def _statement(self, connection: sqlite3.Connection, frame: dict) -> dict:
        sql = frame["sql"]
        params = remote.decode_params(frame.get("params"))
        kind = remote.statement(sql)
        if kind == "write" and not connection.in_transaction:
            return self._begin_write(connection, frame.get("rid"), sql, params)
        if kind == "read" and not connection.in_transaction:
            return self._begin_read(connection, frame.get("rid"), sql, params)
        if kind == "commit":
            return self._commit(connection, frame.get("rid"), lambda: connection.execute(sql, params))
        if kind is not None or VACUUM.match(sql) and not connection.in_transaction:
            # ROLLBACK, or a BEGIN inside a transaction, which SQLite refuses.
            # A VACUUM (a backup's `VACUUM INTO`) runs its own BEGIN and
            # COMMIT, which the authorizer sees; SQLite refuses it inside a
            # transaction, so they commit nothing of the session's. One
            # behind a comment is refused.
            with self._routed():
                return _answer(connection, connection.execute(sql, params))
        return _answer(connection, connection.execute(sql, params))

    def _begin_write(self, connection: sqlite3.Connection, rid, sql: str, params) -> dict:
        """Own R from before the statement runs; an id seen before is refused."""
        if rid is None:
            raise remote.RemoteError(
                f"refused: {' '.join(sql.split())!r} opens a write transaction, which needs a "
                f"request id; this client sent none"
            )
        rid = _request_id(rid)
        owners = self.server.owners  # type: ignore[attr-defined]
        with owners.lock(rid):
            if not owners.admit(rid, self.number):
                raise remote.RemoteError(f"refused: request id {rid} was used before; "
                                         f"a write transaction takes a new id")
            self.wrote = False
            try:
                with self._routed():
                    answer = _answer(connection, connection.execute(sql, params))
                if self._recorded(connection, rid):
                    raise remote.RemoteError(f"refused: request id {rid} has committed before; "
                                             f"a write transaction takes a new id")
            except BaseException:
                if connection.in_transaction:
                    self._quiet(connection, "ROLLBACK")
                owners.settle(rid)
                raise
            self.kind, self.rid, self.mark = "write", rid, connection.total_changes
        return answer

    def _begin_read(self, connection: sqlite3.Connection, rid, sql: str, params) -> dict:
        """A plain `BEGIN`: one snapshot, no id, no row, nothing written (gap C2)."""
        if rid is not None:
            raise remote.RemoteError(
                "refused: a plain BEGIN opens a read transaction, which carries no request id; "
                "a write transaction begins IMMEDIATE"
            )
        before = int(self._quiet(connection, "PRAGMA query_only").fetchone()[0])
        self._quiet(connection, "PRAGMA query_only = ON")
        try:
            with self._routed():
                answer = _answer(connection, connection.execute(sql, params))
        except BaseException:
            self._quiet(connection, f"PRAGMA query_only = {before}")
            raise
        self.kind, self.query_only = "read", before
        return answer

    def _commit(self, connection: sqlite3.Connection, rid, run) -> dict:
        """`COMMIT`: from R's owner it records R inside the transaction first."""
        if self.kind == "write":
            if rid is None:
                raise remote.RemoteError(
                    f"refused: COMMIT of write transaction {self.rid} carries no request id; "
                    f"nothing was committed"
                )
            if rid != self.rid:
                raise remote.RemoteError(
                    f"refused: COMMIT names request {rid}, which this session does not own; "
                    f"nothing was committed"
                )
            with self.server.owners.lock(rid):  # type: ignore[attr-defined]
                if _fault is not None:
                    _fault("before-commit", self)
                # A transaction that changed nothing gets no row: committed
                # or not, it wrote nothing, so `absent` and its "run the verb
                # again" stay true, and a quiet tick adds no row to the hub.
                # The row is looked for at each attempt, never remembered: a
                # failed COMMIT leaves the transaction open, and a ROLLBACK TO
                # may since have removed the row it inserted.
                if (self.wrote or connection.total_changes != self.mark) and not self._recorded(connection, rid):
                    self._quiet(connection, "INSERT INTO request_outcome (id, committed_at) VALUES (?, ?)",
                                (rid, now()))
                    self.hidden += 1
                with self._routed():
                    answer = _answer(connection, run())
                if _fault is not None:
                    _fault("after-commit", self)
                self._settle(connection)
            return answer
        if rid is not None:
            # A COMMIT for an id this session never owned: refused whatever
            # the session holds, so only the owner can commit R.
            raise remote.RemoteError(
                f"refused: COMMIT names request {rid}, which this session does not own; "
                f"nothing was committed"
            )
        with self._routed():
            return _answer(connection, run())

    def _settle(self, connection: sqlite3.Connection | None) -> None:
        """End the session's hold once its transaction has ended, however it ended."""
        if self.kind is None or (connection is not None and connection.in_transaction):
            return
        if self.kind == "write":
            owners = self.server.owners  # type: ignore[attr-defined]
            with owners.lock(self.rid):
                owners.settle(self.rid)
        elif connection is not None:
            self._quiet(connection, f"PRAGMA query_only = {self.query_only}")
        self.kind, self.rid = None, None

    def _adopt(self, connection: sqlite3.Connection) -> None:
        """Hold a transaction the hub did not open as a read.

        Only a `SAVEPOINT` outside a transaction opens one: `_authorize`
        refuses every other way. The library reads a snapshot that way, and
        the RELEASE that ends it would commit any write without an id.
        """
        if connection.in_transaction and self.kind is None:
            self.query_only = int(self._quiet(connection, "PRAGMA query_only").fetchone()[0])
            self._quiet(connection, "PRAGMA query_only = ON")
            self.kind = "read"

    def _outcome(self, connection: sqlite3.Connection, rid) -> str:
        rid = _request_id(rid)
        if connection.in_transaction:
            raise remote.RemoteError("refused: ask for an outcome outside a transaction")
        owners = self.server.owners  # type: ignore[attr-defined]
        with owners.lock(rid):
            if owners.owner(rid) is not None:
                return remote.IN_FLIGHT
            return remote.RECORDED if self._recorded(connection, rid) else remote.ABSENT

    def handle(self) -> None:
        with Session.counter_guard:
            Session.counter += 1
            self.number = Session.counter
        server: Server = self.server  # type: ignore[assignment]
        sock: socket.socket = self.request
        connection = None
        image = b""
        traced = self.traced
        # Gap tracking for the write transaction open on this session.
        opened_at = last_answer = 0.0
        frames = 0
        gap = 0.0
        gap_before = ""
        first_sql = ""
        try:
            while True:
                # Silence inside an open transaction is bounded; outside one
                # it holds nothing a hub writer waits on.
                idle = connection is not None and connection.in_transaction
                sock.settimeout(server.idle_timeout if idle else None)
                try:
                    frame = remote.read_frame(sock)
                except TimeoutError:
                    server.log.line(f"session {self.number} silent {server.idle_timeout:g} s inside a "
                                    f"transaction; closed and rolled back")
                    return
                except (EOFError, OSError):
                    return
                sock.settimeout(None)
                received = time.monotonic()
                self.denied = None
                writing = connection is not None and first_sql != ""
                if writing:
                    frames += 1
                    if received - last_answer >= gap:
                        gap = received - last_answer
                        gap_before = _brief(frame)
                try:
                    op = frame.get("op")
                    opening = connection is None and op == "open"
                    if frame.get("v") != remote.PROTOCOL_VERSION:
                        if opening:
                            # The first frame of an older or newer client:
                            # name the side to upgrade (step 3).
                            raise remote.BuildMismatch("protocol", frame.get("v"), remote.PROTOCOL_VERSION)
                        raise remote.RemoteError(
                            f"protocol version {frame.get('v')!r} refused; this hub speaks "
                            f"{remote.PROTOCOL_VERSION}"
                        )
                    if connection is None:
                        if op != "open":
                            raise remote.RemoteError(f"the first frame must be open, not {op!r}")
                        # Loopback is not a user boundary: another local
                        # account reaches 127.0.0.1 too. The token file is
                        # owner-only, so a client that read it is the owner.
                        if not server.token or not hmac.compare_digest(str(frame.get("token") or ""), server.token):
                            raise remote.RemoteError(
                                f"refused: the open frame lacks this server's token; read it from "
                                f"{server.token_file}"
                            )
                        # Step 3: the same build on both sides, or nothing
                        # opens. The file's schema is still checked against
                        # this library by `open_local` below; this checks the
                        # client's build against this library.
                        remote.check_handshake(frame)
                        target = Path(frame["path"]) if frame.get("path") else server.database
                        if frame.get("create"):
                            # init and migrate are hub verbs; a session never
                            # creates a database (gap (h)).
                            raise remote.RemoteError(
                                f"a remote session cannot create a database; run "
                                f"`local-sd-db/sd-db.sh init` on the hub ({target})"
                            )
                        connection = database.open_local(
                            target, write=bool(frame.get("write", True)),
                            create=bool(frame.get("create", False)),
                            busy_timeout=int(frame.get("busy_timeout", database.BUSY_TIMEOUT)),
                            cached_statements=0,
                        )
                        connection.set_authorizer(self._authorize)
                        answer = _answer(connection, None)
                    elif op == "execute":
                        sql = frame["sql"]
                        answer = self._statement(connection, frame)
                        if not writing and connection.in_transaction and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                            first_sql, frames, gap, gap_before, opened_at = sql.strip(), 0, 0.0, "", received
                    elif op == "executemany":
                        answer = _answer(connection, connection.executemany(
                            frame["sql"], [remote.decode_params(p) for p in frame["params"]]
                        ))
                    elif op == "executescript":
                        if self.kind is not None:
                            # `executescript` commits the open transaction
                            # before it runs: no row for a write, and a read
                            # left under `query_only`.
                            raise remote.RemoteError(
                                f"refused: executescript would commit the open {self.kind} "
                                f"transaction without its COMMIT; end it first"
                            )
                        # A script carries no R, like an autocommit write, so
                        # its own BEGIN and COMMIT are routed (the restore
                        # path replays a migration that way). It must end
                        # what it opens: a transaction it left open would be
                        # one no frame holds.
                        try:
                            with self._routed():
                                connection.executescript(frame["sql"])
                        finally:
                            left = connection.in_transaction
                            if left:
                                self._quiet(connection, "ROLLBACK")
                        if left:
                            raise remote.RemoteError("refused: the script left a transaction open; "
                                                     "it was rolled back, so nothing in it was written")
                        answer = _answer(connection, None)
                    elif op == "commit":
                        answer = self._commit(connection, frame.get("rid"), connection.commit)
                    elif op == "rollback":
                        with self._routed():
                            connection.rollback()
                        answer = _answer(connection, None)
                    elif op == "outcome":
                        value = self._outcome(connection, frame.get("rid"))
                        server.log.line(f"session {self.number} outcome of {frame.get('rid')}: {value}")
                        answer = {**_answer(connection, None), "value": value}
                    elif op == "trace":
                        connection.set_trace_callback(traced.append if frame.get("on") else None)
                        answer = _answer(connection, None)
                    elif op == "total_changes":
                        answer = {**_answer(connection, None),
                                  "value": connection.total_changes - self.hidden}
                    elif op == "serialize":
                        # The size now, the bytes in `chunk` answers, so no
                        # frame carries more than `remote.CHUNK` of them.
                        image = connection.serialize(name=frame.get("name", "main"))
                        answer = {**_answer(connection, None), "value": len(image)}
                    elif op == "chunk":
                        offset = int(frame["offset"])
                        length = max(1, min(int(frame.get("length", remote.CHUNK)), remote.CHUNK))
                        piece = image[offset:offset + length]
                        if offset + len(piece) >= len(image):
                            image = b""
                        answer = {**_answer(connection, None), "value": remote.encode_value(piece)}
                    elif op == "registry":
                        # Seam 7: the one file beside the database a remote
                        # session reads, staged like an image so `chunk`
                        # serves it. The satellite stage installs it (step 9).
                        from .errors import RegistryError
                        from .registry import REGISTRY_NAME

                        beside = target.parent / REGISTRY_NAME
                        try:
                            image = beside.read_bytes()
                        except FileNotFoundError:
                            raise RegistryError(f"no provider registry at {beside} on the hub") from None
                        answer = {**_answer(connection, None), "value": len(image)}
                    elif op == "iterdump":
                        answer = {**_answer(connection, None), "value": list(connection.iterdump())}
                    elif op == "close":
                        connection.close()
                        self._settle(None)
                        remote.send_frame(sock, {"v": remote.PROTOCOL_VERSION, "rid": frame.get("rid"), "ok": True, "in_transaction": False})
                        connection = None
                        return
                    else:
                        raise remote.RemoteError(f"unknown operation {op!r}")
                except Exception as error:  # every failure goes back as a frame
                    if self.denied is not None:
                        # SQLite says only "not authorized"; `_authorize` said why.
                        error = remote.RemoteError(self.denied)
                    answer = {"ok": False, "error": remote.describe_error(error)}
                    if connection is not None:
                        answer["in_transaction"] = connection.in_transaction
                    elif isinstance(error, (remote.BuildMismatch, remote.HubRestartNeeded)):
                        server.log.line(f"session {self.number} refused before open: {error}")
                if connection is not None:
                    # A ROLLBACK, or a transaction SQLite ended on an error.
                    self._settle(connection)
                    self._adopt(connection)
                answer["v"] = remote.PROTOCOL_VERSION
                answer["rid"] = frame.get("rid") if isinstance(frame, dict) else None
                if traced:
                    answer["trace"] = traced[:]
                    traced.clear()
                if first_sql and connection is not None and not connection.in_transaction:
                    server.log.transaction(self.number, frames, gap, time.monotonic() - opened_at, gap_before)
                    first_sql = ""
                try:
                    remote.send_frame(sock, answer)
                except remote.RemoteError as error:
                    # An answer too large for one frame goes back as an error,
                    # and the session stays open.
                    refused = {"v": remote.PROTOCOL_VERSION, "rid": answer.get("rid"), "ok": False,
                               "error": remote.describe_error(error)}
                    if connection is not None:
                        refused["in_transaction"] = connection.in_transaction
                    try:
                        remote.send_frame(sock, refused)
                    except OSError:
                        return
                except OSError:
                    return
                last_answer = time.monotonic()
        finally:
            if connection is not None:
                if first_sql:
                    server.log.line(f"session {self.number} closed inside a write transaction; rolled back")
                connection.close()
            # Closed, so rolled back: the id is no longer owned, and
            # `outcome` answers from the file from here on.
            self._settle(None)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, database_path: Path, log: Log, token: str, token_file: Path,
                 idle_timeout: float = IDLE_TIMEOUT) -> None:
        self.database = database_path
        self.log = log
        self.token = token
        self.token_file = token_file
        self.idle_timeout = idle_timeout
        self.owners = Owners()
        super().__init__((LOOPBACK, port), Session)


def _write_token(path: Path) -> str:
    """A fresh token in a file only this account can read (mode 0600)."""
    token = secrets.token_urlsafe(32)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    return token


def read_token(path: Path) -> str:
    """The token a client sends, from the server's token file."""
    return Path(path).read_text().strip()


def serve(port: int, database_path: Path, stream=sys.stderr) -> int:
    log = Log(stream)
    # Resolved, so the log names the file itself and not a link to it (gap (h)).
    target = database_path.expanduser().resolve()
    if not target.exists():
        # Never create: a server under the wrong home must fail closed rather
        # than serve a second, empty database (gap (h)).
        log.line(f"no database at {target}; run `local-sd-db/sd-db.sh init`")
        return 1
    resolved = target.resolve()
    guard = resolved.with_name(resolved.name + LOCK_SUFFIX)
    token_file = resolved.with_name(resolved.name + TOKEN_SUFFIX)
    try:
        with lock(guard, blocking=False, noun="serve",
                  held=f"another server owns this database: {guard}"):
            # Bind before the token exists, so a refused port leaves no token.
            try:
                server = Server(port, target, log, "", token_file)
            except OSError as error:
                log.line(f"cannot listen on {LOOPBACK}:{port}: {error.strerror or error}")
                return 1
            try:
                # The build this process loaded, taken before any session
                # can ask for it (sd:1480).
                remote.build_digest()
                server.token = _write_token(token_file)
                host, bound = server.server_address[:2]
                log.line(f"serving {target} on {host}:{bound}; token in {token_file}")

                def stop(signum, _frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                server.serve_forever(poll_interval=0.2)
            finally:
                server.server_close()
                token_file.unlink(missing_ok=True)
                log.line(log.summary())
    except RunnerRefused as refused:
        log.line(str(refused))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sd-db.sh serve")
    parser.add_argument("--loopback", action="store_true",
                        help="bind 127.0.0.1; the only binding until peer identity lands")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--database", type=Path, default=None)
    arguments = parser.parse_args(argv)
    if not arguments.loopback:
        print("sd-db serve: pass --loopback; serving another address waits for peer identity "
              "(step 7 of the second-machine plan)", file=sys.stderr)
        return 1
    # The file on this machine, never the hub-aware default: a server does
    # not serve what it would reach through `hub.json`.
    return serve(arguments.port, arguments.database or database.local_path())


if __name__ == "__main__":
    raise SystemExit(main())
