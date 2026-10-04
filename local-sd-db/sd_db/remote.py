"""A connection to a database served by `sd_db.serve`, over one TCP socket.

The satellite half of the hub/satellite design
(`docs/work/2026-09-22-run-the-framework-from-a-second-machine/`). The hub
holds the file and opens it with the library's own local open; this class
forwards each statement and rebuilds the answer. It carries exactly the
connection and cursor surface the library touches, which
`sd_db.testing.surface` names and proves against the whole suite; anything
else raises `AttributeError`.

Steps 2 to 4 of the plan: loopback only. The `open` frame carries the
owner-only token the server wrote beside the database; peer identity for
another machine waits for step 7. It also carries the handshake of step 3:
this build's package version and `SCHEMA_VERSION`, beside the protocol
version every frame carries. The hub refuses any difference with
`BuildMismatch` before it opens anything. The frame already carries the
request id field (`rid`, `None` until step 5 gives write transactions an
id), so later steps add checks and not fields.

The frame is a 4-byte big-endian length and that many bytes of UTF-8 JSON.
JSON and not pickle: the hub will read frames from another machine, and a
pickle is code.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import sqlite3
import struct
import tempfile
from pathlib import Path

from . import errors as _errors

#: Bumped when a frame changes shape. The server refuses any other.
PROTOCOL_VERSION = 1

#: A frame larger than this is refused on both sides: a corrupt length
#: prefix must not become a 4 GiB allocation.
MAX_FRAME = 256 * 1024 * 1024
#: Bytes of a serialized image per `chunk` answer; base64 keeps it under `MAX_FRAME`.
CHUNK = 64 * 1024 * 1024

_HEADER = struct.Struct(">I")


class RemoteError(_errors.SdDbError):
    """The hub raised something this side cannot rebuild, or the wire broke."""


class HubUnreachable(RemoteError):
    """No session could be opened, or the session ended mid-request."""

    def __init__(self, host: str, port: int, reason: str) -> None:
        self.host = host
        self.port = port
        self.reason = reason
        super().__init__(f"the sd hub at {host}:{port} is unreachable: {reason}")


#: How the refusal names each of the three handshake fields.
DESCRIBED = {"protocol": "protocol version", "package": "sd_db package version",
             "schema": "SCHEMA_VERSION", "build": "build digest"}


class BuildMismatch(RemoteError):
    """The two sides run different `sd_db` builds; the hub opened nothing.

    Raised by the hub in answer to the first frame, and rebuilt on the
    satellite with the hub's text. A schema number alone is not the check:
    two builds can share `SCHEMA_VERSION` and send different SQL, so the
    package version is compared too, and the protocol version before both.
    `upgrade` names the side whose build is older, `hub` or `satellite`,
    or `both` when the two differ without an order.
    """

    def __init__(self, field: str, satellite, hub) -> None:
        self.field = field
        self.satellite = str(satellite)
        self.hub = str(hub)
        self.upgrade = older(satellite, hub)
        if self.upgrade == "hub":
            remedy = f"upgrade the hub's sd_db to {satellite}"
        elif self.upgrade == "satellite":
            remedy = f"upgrade this satellite's sd_db to {hub}"
        else:
            remedy = "install the same sd_db build on the hub and on this satellite"
        super().__init__(
            f"sd_db build mismatch: {DESCRIBED[field]} {satellite} on this satellite, "
            f"{hub} on the hub; {remedy}. The hub ran no statement"
        )


def _numbers(version) -> tuple[int, ...] | None:
    text = str(version)
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(part) for part in text.split("."))


def older(satellite, hub) -> str:
    """The side to upgrade: the one whose version sorts lower."""
    ours, theirs = _numbers(satellite), _numbers(hub)
    if ours is None or theirs is None or ours == theirs:
        return "both"
    return "satellite" if ours < theirs else "hub"


_digest: str | None = None


def _hash_files() -> str:
    """This package's Python and SQL as they are on disk right now."""
    import hashlib

    root = Path(__file__).resolve().parent
    hashed = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.suffix in (".py", ".sql")
                       and p.is_file() and "__pycache__" not in p.parts):
        hashed.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        hashed.update(path.read_bytes() + b"\0")
    return hashed.hexdigest()[:16]


def build_digest() -> str:
    """A digest of this package's own files: the build, whatever its version.

    The package version has not been bumped per build, so two builds with
    different SQL can share it. The digest covers the package's Python and
    its SQL migrations, so any difference in what the two sides would run
    refuses. Nothing else counts, so a stray `.DS_Store` cannot.

    Computed once per process and kept: it names the code this process
    loaded. `serve` takes it before it listens (sd:1480), so an install
    under a running hub cannot change what the hub says it runs.
    """
    global _digest
    if _digest is None:
        _digest = _hash_files()
    return _digest


class HubRestartNeeded(RemoteError):
    """The hub's `sd_db` was installed again after the hub started; it opened nothing.

    The running hub holds the code it loaded at start, and its files now
    hold another build. Neither digest can be compared honestly with the
    satellite's, so every session is refused until the hub restarts.
    """

    def __init__(self, running: str, installed: str) -> None:
        self.running = running
        self.installed = installed
        super().__init__(
            f"the hub's sd_db was installed after the hub started: it runs build {running}, "
            f"its files hold build {installed}; restart `sd-db.sh serve` on the hub. "
            f"The hub ran no statement"
        )


def handshake() -> dict:
    """This build, as the first frame states it (step 3).

    Read at call time, not at import: the package has finished importing by
    then, and a test can stand in for another build.
    """
    from . import __version__, schema

    return {"package": __version__, "schema": schema.SCHEMA_VERSION, "build": build_digest()}


def check_handshake(frame: dict) -> None:
    """The hub's side: refuse any build but its own, protocol first.

    The package and the schema come before the digest because their order
    names the side to upgrade; the digest has no order.
    """
    if frame.get("v") != PROTOCOL_VERSION:
        raise BuildMismatch("protocol", frame.get("v"), PROTOCOL_VERSION)
    # Before the fields: a hub whose files moved under it would compare a
    # build it does not run, and name the wrong remedy.
    installed = _hash_files()
    if installed != build_digest():
        raise HubRestartNeeded(build_digest(), installed)
    ours = handshake()
    for field in ("package", "schema", "build"):
        if frame.get(field) != ours[field]:
            raise BuildMismatch(field, frame.get(field), ours[field])


# -- frames ----------------------------------------------------------------


def encode_value(value):
    """One SQL value as JSON. Bytes are the one type JSON lacks."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def decode_value(value):
    if isinstance(value, dict) and "$b" in value:
        return base64.b64decode(value["$b"])
    return value


def encode_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return {"$map": {key: encode_value(value) for key, value in params.items()}}
    return [encode_value(value) for value in params]


def decode_params(params):
    if params is None:
        return ()
    if isinstance(params, dict):
        return {key: decode_value(value) for key, value in params["$map"].items()}
    return [decode_value(value) for value in params]


def send_frame(sock: socket.socket, frame: dict) -> None:
    body = json.dumps(frame, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise RemoteError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    sock.sendall(_HEADER.pack(len(body)) + body)


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    chunks = []
    while size:
        chunk = sock.recv(min(size, 1 << 20))
        if not chunk:
            raise EOFError("the peer closed the session")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket) -> dict:
    (size,) = _HEADER.unpack(_read_exactly(sock, _HEADER.size))
    if size > MAX_FRAME:
        raise RemoteError(f"frame of {size} bytes exceeds {MAX_FRAME}")
    return json.loads(_read_exactly(sock, size).decode("utf-8"))


def request(op: str, *, rid: str | None = None, **fields) -> dict:
    """A request frame. Every one carries the version and the id field."""
    return {"v": PROTOCOL_VERSION, "rid": rid, "op": op, **fields}


# -- errors ----------------------------------------------------------------


def describe_error(error: BaseException) -> dict:
    """An exception as the hub sends it: enough to raise the same one here."""
    kind = type(error)
    described = {
        "module": kind.__module__,
        "name": kind.__qualname__,
        "args": [a if isinstance(a, (str, int, float, bool, type(None))) else str(a) for a in error.args],
        "text": str(error),
    }
    attributes = {
        key: value for key, value in vars(error).items()
        if isinstance(value, (str, int, float, bool, type(None)))
    }
    for key in ("sqlite_errorcode", "sqlite_errorname"):
        if hasattr(error, key):
            attributes[key] = getattr(error, key)
    if attributes:
        described["attributes"] = attributes
    return described


def _error_class(module: str, name: str):
    if module in ("builtins", "sqlite3") or module == "sd_db" or module.startswith("sd_db."):
        try:
            import importlib

            found = importlib.import_module(module)
            for part in name.split("."):
                found = getattr(found, part)
        except (ImportError, AttributeError):
            return None
        if isinstance(found, type) and issubclass(found, BaseException):
            return found
    return None


def rebuild_error(described: dict) -> BaseException:
    """The exception the hub raised, as the same class with the same text.

    `sqlite3` and builtin errors are rebuilt with their arguments. A library
    error is rebuilt without calling its `__init__`, whose arguments are not
    its message: `SchemaTooNew(found, built_for)` is rebuilt from `args` and
    its attributes. Anything else is a `RemoteError` naming the class.
    """
    kind = _error_class(described["module"], described["name"])
    attributes = described.get("attributes", {})
    if kind is None:
        return RemoteError(f"{described['module']}.{described['name']}: {described['text']}")
    if kind.__module__ in ("builtins", "sqlite3"):
        try:
            error = kind(*described["args"])
        except TypeError:
            error = kind(described["text"])
    else:
        error = kind.__new__(kind)
        error.args = tuple(described["args"])
    for key, value in attributes.items():
        try:
            setattr(error, key, value)
        except AttributeError:
            pass
    return error


# -- rows ------------------------------------------------------------------

_shapes: dict[tuple[str, ...], sqlite3.Cursor] = {}


def _shape(names: tuple[str, ...]) -> sqlite3.Cursor:
    """A cursor whose description is `names`, so `sqlite3.Row` can use it.

    `sqlite3.Row(cursor, values)` takes its keys from the cursor's
    description. Building real `sqlite3.Row` objects keeps every behaviour a
    caller relies on -- `row["name"]`, `row[0]`, `keys()`, equality, `dict()` --
    identical to a local connection, instead of a look-alike class that would
    drift. The cursor belongs to a private in-memory database, not the hub's.
    """
    cursor = _shapes.get(names)
    if cursor is None:
        columns = ", ".join('NULL AS "' + name.replace('"', '""') + '"' for name in names) or "NULL"
        memory = sqlite3.connect(":memory:", check_same_thread=False)
        cursor = memory.execute(f"SELECT {columns} WHERE 0")
        _shapes[names] = cursor
    return cursor


class Cursor:
    """The answer to one statement, fetched whole by the hub."""

    arraysize = 1

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._rows: list = []
        self._position = 0

    def _load(self, answer: dict) -> Cursor:
        described = answer.get("description")
        self.description = (
            tuple((name, None, None, None, None, None, None) for name in described)
            if described is not None else None
        )
        self.lastrowid = answer.get("lastrowid")
        self.rowcount = answer.get("rowcount", -1)
        factory = self.connection.row_factory
        rows = [tuple(decode_value(value) for value in row) for row in answer.get("rows", [])]
        if factory is sqlite3.Row and described is not None:
            shape = _shape(tuple(described))
            rows = [sqlite3.Row(shape, row) for row in rows]
        elif factory is not None:
            rows = [factory(self, row) for row in rows]
        self._rows = rows
        self._position = 0
        return self

    def execute(self, sql: str, parameters=()) -> Cursor:
        return self._load(self.connection._call("execute", sql=sql, params=encode_params(parameters)))

    def executemany(self, sql: str, seq_of_parameters) -> Cursor:
        return self._load(self.connection._call(
            "executemany", sql=sql, params=[encode_params(p) for p in seq_of_parameters],
        ))

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchmany(self, size: int | None = None) -> list:
        size = self.arraysize if size is None else size
        rows = self._rows[self._position:self._position + size]
        self._position += len(rows)
        return rows

    def fetchall(self) -> list:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        self._rows = []
        self._position = 0


class Connection:
    """A hub-side `sqlite3.Connection`, reached over one socket.

    One session is one hub connection: `in_transaction` is the hub's, sent
    back with every answer, and closing the socket makes SQLite roll back
    whatever the session left open.
    """

    def __init__(self, host: str, port: int, *, path: Path | str | None, write: bool,
                 create: bool, busy_timeout: int, token,
                 timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.row_factory = sqlite3.Row
        self.in_transaction = False
        self._socket = None
        self._trace = None
        try:
            self._socket = socket.create_connection((host, port), timeout=timeout)
        except OSError as error:
            raise HubUnreachable(host, port, error.strerror or str(error)) from error
        self._socket.settimeout(None)
        # The token proves the client can read the server's owner-only token
        # file, so a loopback session stays inside the owner's account. A
        # callable is read only once the socket is up: the server deletes
        # its token file when it stops, and a stopped hub is unreachable,
        # not misconfigured.
        if callable(token):
            try:
                token = token()
            except BaseException:
                self._abandon()
                raise
        self._call("open", path=None if path is None else str(path), write=write,
                   create=create, busy_timeout=busy_timeout, token=token, **handshake())

    def _call(self, op: str, **fields) -> dict:
        if self._socket is None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        try:
            send_frame(self._socket, request(op, **fields))
            answer = read_frame(self._socket)
        except (OSError, EOFError) as error:
            self._abandon()
            raise HubUnreachable(self.host, self.port, str(error)) from error
        if "in_transaction" in answer:
            self.in_transaction = answer["in_transaction"]
        if self._trace is not None:
            for statement in answer.get("trace", ()):
                self._trace(statement)
        if not answer.get("ok"):
            if op == "open":
                self._abandon()
            raise rebuild_error(answer["error"])
        return answer

    def _abandon(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def cursor(self) -> Cursor:
        if self._socket is None:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return Cursor(self)

    def execute(self, sql: str, parameters=()) -> Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters) -> Cursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> Cursor:
        cursor = self.cursor()
        self._call("executescript", sql=sql_script)
        return cursor

    @property
    def total_changes(self) -> int:
        return self._call("total_changes")["value"]

    def iterdump(self):
        return iter(self._call("iterdump")["value"])

    def registry_bytes(self) -> bytes:
        """The hub's `providers.yaml`, beside the database this session opened.

        Seam 7 of the design: the registry is the one file beside the
        database a satellite reads. `RegistryError` when the hub has none.
        """
        import io

        size = self._call("registry")["value"]
        sink = io.BytesIO()
        self._fetch(size, sink)
        return sink.getvalue()

    def _fetch(self, size: int, sink) -> None:
        """Read the `size` bytes the hub staged into `sink`, one `chunk` at a time."""
        while sink.tell() < size:
            chunk = decode_value(self._call("chunk", offset=sink.tell(), length=CHUNK)["value"])
            if not chunk:
                raise RemoteError(f"the hub's image ended at {sink.tell()} of {size} bytes")
            sink.write(chunk)

    def set_trace_callback(self, callback) -> None:
        """Traced on the hub, where the statements run, and replayed here.

        The hub's trace is SQLite's own, so the callback sees what a local
        one sees: expanded SQL, one statement at a time.
        """
        self._trace = callback
        self._call("trace", on=callback is not None)

    def backup(self, target: sqlite3.Connection, *, pages: int = -1, progress=None,
               name: str = "main", sleep: float = 0.25) -> None:
        """Copy the hub's database into a connection on this side.

        The hub sends its image (`serialize`); it lands in a private file
        here, and SQLite's own backup copies it into `target` with the
        caller's pages, progress and sleep. A file and not `deserialize`: an
        in-memory image of a WAL database will not open, and patching its
        header would hand `target` a journal mode the hub's file lacks.
        """
        size = self._call("serialize", name=name)["value"]
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder) / "image.db"
            # The image comes in chunks, so its size is not bounded by one
            # frame (`MAX_FRAME`).
            with open(staged, "wb") as image:
                self._fetch(size, image)
            staging = sqlite3.connect(staged)
            try:
                staging.backup(target, pages=pages, progress=progress, sleep=sleep)
            finally:
                staging.close()

    def commit(self) -> None:
        self._call("commit")

    def rollback(self) -> None:
        self._call("rollback")

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            self._call("close")
        except HubUnreachable:
            pass
        self._abandon()
        self.in_transaction = False

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        self._call("commit" if kind is None else "rollback")
        return False

    def __del__(self) -> None:
        try:
            self._abandon()
        except Exception:
            pass


def connect(host: str, port: int, target: Path | str | None, *, token, write: bool = True,
            create: bool = False, busy_timeout: int = 5000,
            timeout: float | None = None) -> Connection:
    """Open a session on the hub at `host:port`.

    `token` is the content of the server's `<database>.serve.token`, which
    only the server's owner can read, or a callable that reads it once the
    socket is up. A `target` of `None` opens the hub's own database.
    """
    return Connection(host, port, path=target, write=write, create=create,
                      busy_timeout=busy_timeout, token=token, timeout=timeout)
