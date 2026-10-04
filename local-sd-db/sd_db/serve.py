"""The hub half: serve one database to `sd_db.remote` connections.

    python -m sd_db.serve [--loopback] [--port N] [--database PATH]

Steps 2 and 7 of `docs/work/2026-09-22-run-the-framework-from-a-second-machine/`.
Each session is one TCP connection and one hub-side connection from the
library's own local open, so a remote statement meets the same pragmas, the
same version refusals and the same SQLite write lock as a local one.

One server per file. The server takes `<database>.serve.lock` beside it
through `runner_journal.lock` before it listens, and a second server over the
same file exits non-zero naming the lock. Ownership of a request id (step 5)
will live in the server's memory, so two servers over one file would be two
registries.

Two listeners, one per run:

* `--loopback` binds 127.0.0.1. Loopback is not a user boundary: every
  local account reaches it. The server writes a fresh token to
  `<database>.serve.token` beside it, mode 0600, and refuses an `open` frame
  without it. A client that sends it could read the owner's file, so it
  runs as the owner or as root.
* Without it, the server binds this node's Tailscale IPv4 address and
  carries no token (R7). It admits a session by its TCP peer, through
  `sd_db.tailnet`: the rules the dashboard's direct listener uses. A tagged
  or expired node, a login other than this node's owner, and this node's
  own addresses are refused before anything opens (R8, criterion 6). The
  last one closes the hub's second-account path: `whois` names a node's
  owner, not the account that dialed (design, Q3 = A).

The `open` frame carries the client's build (step 3): its protocol version,
its package version and its `SCHEMA_VERSION`. The server refuses any
difference from its own with `sd_db.remote.BuildMismatch`, naming the side
to upgrade, before it opens the file.

A loopback session may name the file it opens. That is what lets the
library's suite, which builds a database per test, run over the wire;
behind the token it grants nothing the owner's own processes lack. A
tailnet session opens the served database and nothing else.

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

from . import database, remote, tailnet
from .runner import RunnerRefused
from .runner_journal import lock

LOOPBACK = "127.0.0.1"
#: Suffixes on the database's own file name: one lock and one token per
#: database, so two files in one folder are served side by side.
LOCK_SUFFIX = ".serve.lock"
TOKEN_SUFFIX = ".serve.token"
#: The largest first frame a session may send. An `open` frame is a few
#: hundred bytes, and a peer not yet admitted gets no larger allocation.
OPEN_LIMIT = 64 * 1024


class PeerRefused(remote.RemoteError):
    """The session's TCP peer is not one this listener admits. Nothing opened."""


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
                    frame = remote.read_frame(sock, limit=OPEN_LIMIT if connection is None else remote.MAX_FRAME)
                except (EOFError, OSError):
                    return
                except remote.RemoteError as error:
                    server.log.line(f"session {self.number} dropped: {error}")
                    return
                received = time.monotonic()
                writing = connection is not None and first_sql != ""
                if writing:
                    frames += 1
                    if received - last_answer >= gap:
                        gap = received - last_answer
                        gap_before = _brief(frame)
                refused = False
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
                        server.admit(self.client_address, frame, self.number)
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
                    elif isinstance(error, (remote.BuildMismatch, remote.HubRestartNeeded, PeerRefused)):
                        server.log.line(f"session {self.number} refused before open: {error}")
                        refused = isinstance(error, PeerRefused)
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
                if refused:
                    # A refused peer gets one answer, not a second try.
                    return
                last_answer = time.monotonic()
        finally:
            if connection is not None:
                if first_sql:
                    server.log.line(f"session {self.number} closed inside a write transaction; rolled back")
                connection.close()


class Server(socketserver.ThreadingTCPServer):
    """One listener. `node` is `None` on loopback, where the token admits a
    session; on the tailnet it is this node, and the peer admits it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, database_path: Path, log: Log, token: str, token_file: Path,
                 node: tailnet.Node | None = None) -> None:
        self.database = database_path
        self.log = log
        self.token = token
        self.token_file = token_file
        self.node = node
        super().__init__((LOOPBACK if node is None else str(node.address), port), Session)

    def admit(self, peer, frame: dict, session: int = 0) -> None:
        """Refuse a session before anything opens, and before any SQL runs."""
        if self.node is None:
            # Loopback is not a user boundary: another local account reaches
            # 127.0.0.1 too. The token file is owner-only, so a client that
            # read it is the owner.
            if not self.token or not hmac.compare_digest(str(frame.get("token") or ""), self.token):
                raise remote.RemoteError(
                    f"refused: the open frame lacks this server's token; read it from "
                    f"{self.token_file}"
                )
            return
        refusal = self.refusal(peer, frame)
        shown = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) and len(peer) == 2 else repr(peer)
        if refusal is not None:
            raise PeerRefused(f"refused the peer {shown}: {refusal}; this hub admits only "
                              f"{self.node.login}'s untagged nodes")
        self.log.line(f"session {session} admitted {self.node.login} from {shown}")

    def refusal(self, peer, frame: dict) -> str | None:
        """Why the tailnet listener refuses this peer, or `None`. Order matters:
        the hub's own address is refused before `whois`, which would name the
        operator for any account on this machine."""
        found = tailnet.peer_address(peer)
        if found is None:
            return "not a Tailscale IPv4 address"
        if found[0] in self.node.addresses:
            return ("it is this hub's own Tailscale address, which any account on the hub can dial; "
                    "a process on the hub opens the database directly")
        try:
            identity = tailnet.whois(peer)
        except tailnet.TailnetError as error:
            return str(error)
        if identity.refusal is not None:
            return identity.refusal
        if identity.login != self.node.login:
            return f"the login {identity.login} is not this hub's operator"
        if frame.get("path") is not None:
            return "a tailnet session opens the hub's own database, and names no path"
        return None


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


def serve(port: int, database_path: Path, stream=sys.stderr, *, loopback: bool = True) -> int:
    log = Log(stream)
    target = database_path.expanduser()
    if not target.exists():
        # Never create: a server under the wrong home must fail closed rather
        # than serve a second, empty database (gap (h)).
        log.line(f"no database at {target}; run `local-sd-db/sd-db.sh init`")
        return 1
    node = None
    if not loopback:
        try:
            node = tailnet.this_node()
        except tailnet.TailnetError as error:
            log.line(f"cannot serve on the tailnet: {error}")
            return 1
    resolved = target.resolve()
    guard = resolved.with_name(resolved.name + LOCK_SUFFIX)
    token_file = resolved.with_name(resolved.name + TOKEN_SUFFIX)
    try:
        with lock(guard, blocking=False, noun="serve",
                  held=f"another server owns this database: {guard}"):
            # Bind before the token exists, so a refused port leaves no token.
            address = LOOPBACK if node is None else str(node.address)
            try:
                server = Server(port, target, log, "", token_file, node)
            except OSError as error:
                log.line(f"cannot listen on {address}:{port}: {error.strerror or error}")
                return 1
            try:
                # The build this process loaded, taken before any session
                # can ask for it (sd:1480).
                remote.build_digest()
                host, bound = server.server_address[:2]
                if node is None:
                    server.token = _write_token(token_file)
                    log.line(f"serving {target} on {host}:{bound}; token in {token_file}")
                else:
                    # No token on the tailnet (R7): the peer is the credential.
                    own = ", ".join(sorted(str(value) for value in node.addresses))
                    log.line(f"serving {target} on {host}:{bound} to {node.login}'s untagged nodes; "
                             f"refusing this node's own addresses ({own})")

                def stop(signum, _frame):
                    threading.Thread(target=server.shutdown, daemon=True).start()

                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                server.serve_forever(poll_interval=0.2)
            finally:
                server.server_close()
                if node is None:
                    token_file.unlink(missing_ok=True)
                log.line(log.summary())
    except RunnerRefused as refused:
        log.line(str(refused))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sd-db.sh serve")
    parser.add_argument("--loopback", action="store_true",
                        help="bind 127.0.0.1 and admit by the owner-only token; without it, bind "
                             "this node's Tailscale IPv4 address and admit by `tailscale whois`")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--database", type=Path, default=None)
    arguments = parser.parse_args(argv)
    # The file on this machine, never the hub-aware default: a server does
    # not serve what it would reach through `hub.json`.
    return serve(arguments.port, arguments.database or database.local_path(), loopback=arguments.loopback)


if __name__ == "__main__":
    raise SystemExit(main())
