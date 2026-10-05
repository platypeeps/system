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
will live in the server's memory, so two servers over one file would be two
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

LOOPBACK = "127.0.0.1"
#: Suffixes on the database's own file name: one lock and one token per
#: database, so two files in one folder are served side by side.
LOCK_SUFFIX = ".serve.lock"
TOKEN_SUFFIX = ".serve.token"


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

    def handle(self) -> None:
        with Session.counter_guard:
            Session.counter += 1
            self.number = Session.counter
        server: Server = self.server  # type: ignore[assignment]
        sock: socket.socket = self.request
        connection = None
        image = b""
        traced: list[str] = []
        # Gap tracking for the write transaction open on this session.
        opened_at = last_answer = 0.0
        frames = 0
        gap = 0.0
        gap_before = ""
        first_sql = ""
        try:
            while True:
                try:
                    frame = remote.read_frame(sock)
                except (EOFError, OSError):
                    return
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
                        answer = _answer(connection, connection.execute(sql, remote.decode_params(frame.get("params"))))
                        if not writing and connection.in_transaction and sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                            first_sql, frames, gap, gap_before, opened_at = sql.strip(), 0, 0.0, "", received
                    elif op == "executemany":
                        answer = _answer(connection, connection.executemany(
                            frame["sql"], [remote.decode_params(p) for p in frame["params"]]
                        ))
                    elif op == "executescript":
                        connection.executescript(frame["sql"])
                        answer = _answer(connection, None)
                    elif op in ("commit", "rollback"):
                        getattr(connection, op)()
                        answer = _answer(connection, None)
                    elif op == "trace":
                        connection.set_trace_callback(traced.append if frame.get("on") else None)
                        answer = _answer(connection, None)
                    elif op == "total_changes":
                        answer = {**_answer(connection, None), "value": connection.total_changes}
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


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, database_path: Path, log: Log, token: str, token_file: Path) -> None:
        self.database = database_path
        self.log = log
        self.token = token
        self.token_file = token_file
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
