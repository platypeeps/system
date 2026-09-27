"""Catalog and bounded execution evidence travel with a database snapshot."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .database import connect
from .errors import BackupError
from .runner_exec import MAX_OUTPUT

MANIFEST = "execution-evidence.json"


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _logs(directory):
    if not directory.exists() and not directory.is_symlink():
        return {}
    details = directory.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise BackupError("execution evidence directory must be private and owned")
    result = {}
    for path in sorted(directory.iterdir()):
        details = path.lstat()
        if (not re.fullmatch(r"[a-f0-9]{32}\.(log|receipt\.json)", path.name) or not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid() or details.st_mode & 0o077 or details.st_size > MAX_OUTPUT):
            raise BackupError("execution evidence contains an unsafe or oversized log")
        result[path.name] = path.read_bytes()
    return result


def _expired(value):
    """A note the nightly prune marked: its output left by retention, not by loss."""
    return isinstance(value.get("output_expired"), str)


def _notes(connection):
    result = []
    for row in connection.execute("SELECT id,body,output_path,ended FROM note WHERE kind='exec'"):
        try:
            value = json.loads(row["body"])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("note") != row["id"]:
            continue
        path = Path(row["output_path"] or "")
        if not path.is_absolute() or path.parent.name != "executions" or not re.fullmatch(r"[a-f0-9]{32}\.log", path.name) or value.get("output_path") != str(path):
            raise BackupError("palette note has no matching execution output authority")
        result.append((dict(row), value))
    return result


def capture(source, directory):
    """Copy every log, including evidence newer than the database snapshot."""
    logs = _logs(source.parent / "executions")
    connection = connect(directory / source.name, write=False)
    try:
        notes = _notes(connection)
    finally:
        connection.close()
    if not logs and not notes and not (directory / "commands.yaml").exists():
        return False
    for row, value in notes:
        if row["ended"] and not _expired(value) and Path(row["output_path"]).name not in logs:
            raise BackupError("completed palette execution is missing its output evidence")
    target = directory / "executions"
    target.mkdir(mode=0o700)
    for name, data in logs.items():
        _write(target / name, data)
    if _logs(target) != logs or _logs(source.parent / "executions") != logs:
        raise BackupError("execution output changed during backup")
    catalog = directory / "commands.yaml"
    manifest = {"version": 1, "logs": {name: _hash(data) for name, data in logs.items()},
                "notes": {str(row["id"]): {"body_sha256": _hash(row["body"].encode()),
                    "output_path": row["output_path"]} for row, _ in notes},
                "catalog_sha256": _hash(catalog.read_bytes()) if catalog.exists() else None}
    _write(directory / MANIFEST, (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return True


def _write(path, data):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(directory, state, connection):
    """Validate all evidence before remapping paths in the disposable candidate."""
    state = state.resolve()
    notes = _notes(connection)
    path = directory / MANIFEST
    if not path.exists():
        if notes or (directory / "executions").exists():
            raise BackupError("palette restore needs its execution evidence manifest")
        return {}
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise BackupError("execution evidence manifest is unsafe")
    try:
        manifest = json.loads(path.read_text())
    except (ValueError, UnicodeError) as error:
        raise BackupError("execution evidence manifest is malformed") from error
    logs = _logs(directory / "executions")
    expected_notes = {str(row["id"]): {"body_sha256": _hash(row["body"].encode()),
                      "output_path": row["output_path"]} for row, _ in notes}
    catalog = directory / "commands.yaml"
    catalog_hash = _hash(catalog.read_bytes()) if catalog.exists() and not catalog.is_symlink() else None
    if (not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("notes") != expected_notes
            or manifest.get("logs") != {name: _hash(data) for name, data in logs.items()}
            or manifest.get("catalog_sha256") != catalog_hash):
        raise BackupError("execution evidence or commands catalog differs from its snapshot manifest")
    current = _logs(state / "executions")
    if any(name in current and current[name] != data for name, data in logs.items()):
        raise BackupError("restore refuses to overwrite different live execution evidence")
    for row, value in notes:
        if row["ended"] and not _expired(value) and Path(row["output_path"]).name not in logs:
            raise BackupError("completed palette execution is missing its output evidence")
        relocated = {**value, "output_path": str(state / "executions" / Path(row["output_path"]).name),
                     "registry_path": str(state / "commands.yaml")}
        body = json.dumps(relocated, sort_keys=True)
        connection.execute("UPDATE note SET body=?,output_path=? WHERE id=?", (body, relocated["output_path"], row["id"]))
        connection.execute("UPDATE assignment SET scope=? WHERE role='exec' AND scope=?", ("palette:" + body, "palette:" + json.dumps(value, sort_keys=True)))
    return logs


def install(logs, state):
    """Install missing evidence only; an interrupted copy is safe to retry."""
    if not logs:
        return
    target = state / "executions"
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = _logs(target)
    if any(name in current and current[name] != data for name, data in logs.items()):
        raise BackupError("live execution evidence changed during restore")
    for name, data in logs.items():
        path = target / name
        if name not in current:
            # A killed restore can leave a scratch file, never a partial log.
            with tempfile.NamedTemporaryFile(prefix=".execution-restore-", dir=state) as scratch:
                os.fchmod(scratch.fileno(), 0o600)
                scratch.write(data)
                scratch.flush()
                os.fsync(scratch.fileno())
                try:
                    os.link(scratch.name, path)
                except FileExistsError:
                    pass
        if path.read_bytes() != data:
            raise BackupError("execution evidence readback differs after restore")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
