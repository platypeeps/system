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
  at all. A transaction that changed nothing (no row, no schema, no
  `user_version`) gets no row: it wrote nothing either way, and the table,
  which is never pruned, does not grow on quiet ticks. The server's row
  stays out of the session's `total_changes`. A `COMMIT` naming an R the
  session does not own is refused and not applied, and a write
  transaction's `COMMIT` without its R too.
- `ROLLBACK`, a transaction SQLite ends itself, and the socket closing
  settle ownership.
- `outcome(R)` answers `in_flight` while a session owns R, else `recorded`
  when the row exists, else `absent`. Admission, settlement and the answer
  run under one lock per id, so absence proves the transaction ended
  without committing: nothing unowned can commit.
- A plain `BEGIN` carries no R and opens a read transaction under
  `query_only` (gap C2). Its `COMMIT` or `ROLLBACK` needs no R and writes
  no row.
- A session silent for `IDLE_TIMEOUT` inside an open transaction is closed,
  which rolls it back: below `database.BUSY_TIMEOUT`, so a hub writer
  waiting behind an abandoned satellite gets the lock (gap (g)).
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import signal
import socket
import socketserver
import sqlite3
import sys
import threading
import time
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
        #: The `request_outcome` row is inserted in the open transaction.
        self.recorded = False
        #: What the open write transaction had changed when it began.
        self.mark: tuple = ()
        #: Rows the server inserted, kept out of `total_changes`.
        self.hidden = 0
        #: The connection's own `query_only`, restored when a read ends.
        self.query_only = 0
        self.traced: list[str] = []

    def _quiet(self, connection: sqlite3.Connection, sql: str, params=()) -> sqlite3.Cursor:
        """A statement of the server's own, kept out of the session's trace."""
        mark = len(self.traced)
        try:
            return connection.execute(sql, params)
        finally:
            del self.traced[mark:]

    def _changes(self, connection: sqlite3.Connection) -> tuple:
        """Rows changed, and the schema and user versions: DDL and
        `PRAGMA user_version` move no row but are writes all the same."""
        return (connection.total_changes,
                self._quiet(connection, "PRAGMA schema_version").fetchone()[0],
                self._quiet(connection, "PRAGMA user_version").fetchone()[0])

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
            try:
                answer = _answer(connection, connection.execute(sql, params))
                if self._recorded(connection, rid):
                    raise remote.RemoteError(f"refused: request id {rid} has committed before; "
                                             f"a write transaction takes a new id")
            except BaseException:
                if connection.in_transaction:
                    self._quiet(connection, "ROLLBACK")
                owners.settle(rid)
                raise
            self.kind, self.rid, self.recorded = "write", rid, False
            self.mark = self._changes(connection)
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
                if not self.recorded and self._changes(connection) != self.mark:
                    self._quiet(connection, "INSERT INTO request_outcome (id, committed_at) VALUES (?, ?)",
                                (rid, now()))
                    self.recorded = True
                    self.hidden += 1
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
        self.kind, self.rid, self.recorded, self.mark = None, None, False, ()

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
                        )
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
                        connection.executescript(frame["sql"])
                        answer = _answer(connection, None)
                    elif op == "commit":
                        answer = self._commit(connection, frame.get("rid"), connection.commit)
                    elif op == "rollback":
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
                    answer = {"ok": False, "error": remote.describe_error(error)}
                    if connection is not None:
                        answer["in_transaction"] = connection.in_transaction
                    elif isinstance(error, (remote.BuildMismatch, remote.HubRestartNeeded)):
                        server.log.line(f"session {self.number} refused before open: {error}")
                if connection is not None:
                    # A ROLLBACK, or a transaction SQLite ended on an error.
                    self._settle(connection)
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
    target = database_path.expanduser()
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
