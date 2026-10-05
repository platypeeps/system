"""The backup, and the restore that proves it.

A live WAL database is not a file a file-level mirror can copy consistently.
So the backup is its own step:
`VACUUM INTO` a dated directory, the two configuration files copied beside
it, and then **the copy is opened and checked**, because a backup nobody has
restored is a file, not a backup.

The order is the design:

1. Write a `checkpoint` row, with this run's id, **before** the snapshot.
2. `VACUUM INTO` the dated directory.
3. Copy configuration and the complete validated publication journal beside it.
4. Open the copy. Run `PRAGMA integrity_check`. Find the checkpoint row.
   Compare every table's count against the source.
5. Write the completed backup's ownership manifest. Prune only when requested:
   by count (`keep`) or by age (`keep_days`), never both.

Step 1 before step 2 is what makes step 4 mean something. A checkpoint
written afterwards would be in the source and not in the copy, and its
absence from the copy would prove nothing; written before, it is in both, and
a copy without it is a copy of something other than what was just checked.

**Step 1 is the one conditional step, and step 4 changes with it.** A
database waiting for `migrate` is exactly the one worth snapshotting first,
and the writable open is what refuses it, so `SchemaTooOld` reopens the file
`mode=ro` and no checkpoint row is written. Step 4 then proves the copy by
integrity and counts alone, and step 5 records `checkpoint: false`.
`_owned_backup` never skips the checkpoint question, so such a snapshot is
never owned, never counted for `--keep`, never pruned, and no row prune runs
after it. It restores like any older snapshot: `restore` migrates the staged
copy up to this library's version.

**Failures of the store never depend on the store.** Every function here
raises `BackupError` and writes nothing on the way out. The caller --
`sd-db-backup` -- sends one mail through the path the cron jobs use. That is
why the mail lives in the script and not in this module: a module that
reported its own failure by writing a row would have nothing to report a
failure to write a row.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths
from .database import connect, local_path, refuse_hub_only, schema_version, tables
from .errors import BackupError, SchemaTooOld, SdDbError
from .migrate import migrate as migrate_database
from .schema import SCHEMA_VERSION, TABLES, migrations
from .writes import record_state

#: Where a run writes when no destination is named: the `Backup` folder on the
#: USB disk attached to this Mac. Snapshots taken before 2026-09-25 went to
#: `~/Documents/sd-backups/` and stay there.
DEFAULT_ROOT = Path("/Volumes/local/Backup/sd-backups")

#: The volume `DEFAULT_ROOT` must be on. Detached, `/Volumes/local` is a plain
#: directory on the boot disk, so the default is refused rather than written
#: there (`require_mount`).
DEFAULT_MOUNT = Path("/Volumes/local")

#: Names the root in place of `DEFAULT_ROOT`; a relative value is taken under
#: the home. Like `--destination`, a named root is not asked about the mount.
#: The test packages set it so every injected home keeps its own root.
ROOT_VARIABLE = "SD_DB_BACKUP_ROOT"

#: What is copied beside the database. Identity the rows do not hold.
CONFIGURATION = ("providers.yaml", "commands.yaml")

#: Conventional retention count, used only when retention is requested.
KEEP = 30

#: The name `_dated_directory` gives a run: the day, then an unpadded `.N`
#: when the day already has one. Unpadded means lexical order is not the
#: order the runs happened in -- `.10` sorts before `.9` -- so retention
#: orders by `_directory_order` and never by the name as text.
DATED_NAME = re.compile(r"\d{4}-\d{2}-\d{2}(?:\.[1-9]\d*)?")
BACKUP_MANIFEST = "backup-manifest.json"

# A restore must stop with a usable current database when another process
# holds the writer lock, rather than waiting forever inside sqlite.backup.
RESTORE_SECONDS = 30

#: How long the destination has to finish a test write before a run gives up
#: on it (`probe_destination`).
PROBE_SECONDS = 30

#: The test write, run in a child of its own: open, write and remove one small
#: file in the destination, or in the nearest part of it that exists.
_PROBE = """\
import os, sys
directory = sys.argv[1]
while not os.path.exists(directory) and os.path.dirname(directory) != directory:
    directory = os.path.dirname(directory)
path = os.path.join(directory, f".sd-db-probe-{os.getpid()}")
with open(path, "xb") as handle:
    handle.write(b"probe")
os.remove(path)
"""
PUBLICATION_RESTORE_INTENT = "publication-restore-intent.json"


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_recovery_record(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _files_fingerprint(files: dict[str, bytes]) -> str:
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def _journal_files(directory: Path) -> dict[str, bytes]:
    """Read only a complete, validated publication evidence directory."""
    from .publication_journal import validate_path
    try:
        validate_path(directory)
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}
    except (SdDbError, OSError, ValueError, TypeError) as error:
        raise BackupError(f"publication journal is not a verified recovery source: {error}") from error


def _copy_publications(source: Path, directory: Path, expected: dict[str, int]) -> bool:
    intent = source.parent / PUBLICATION_RESTORE_INTENT
    if intent.exists() or intent.is_symlink():
        raise BackupError("publication journal restore is incomplete; resume its verified backup before taking another")
    journal = source.parent / "publications"
    if not journal.exists() and not journal.is_symlink():
        if expected.get("publication_claim", 0):
            raise BackupError("publication claims exist but their durable journal is missing")
        return False
    _journal_files(journal)
    try:
        shutil.copytree(journal, directory / "publications", symlinks=True)
    except OSError as error:
        raise BackupError(f"publication journal was not copied: {error}") from error
    # A concurrent append may make a copy incomplete. Fail this backup rather
    # than certify a partial catalog or missing event; never change the source.
    _journal_files(directory / "publications")
    return True


#: The migrations that create the two evidence tables. A database at or past
#: the version owes the table; one before it does not have it and is whole.
PUBLICATION_CLAIM_VERSION = 4
RUNNER_RUN_VERSION = 5


def _check_publication_claims(connection: sqlite3.Connection, directory: Path) -> None:
    """Every database claim must have matching, immutable external evidence.

    A table this old library has and the copy does not is nothing to check,
    not a fault: `publication_claim` arrives in migration 004, and `run`
    snapshots a database waiting for `migrate` read-only, so a valid version
    1-3 copy reaches here without it. Unguarded, the `SELECT` left `run` with
    a bare `no such table` from sqlite3 rather than this module's documented
    `BackupError`, on the one database the read-only path exists to serve.

    The gate is the version, not the table's absence. A version 4 or later
    database without the table is malformed: `_check_restore` refuses it, so
    a snapshot of it must not pass here, take an ownership manifest and let
    retention remove a valid recovery point on its account (Codex, sd:1207).
    """
    if schema_version(connection) < PUBLICATION_CLAIM_VERSION:
        return
    if "publication_claim" not in set(tables(connection)):
        raise BackupError(f"a version {schema_version(connection)} database has no publication_claim "
                          f"table; migration {PUBLICATION_CLAIM_VERSION:03d} creates it, so the copy is malformed")
    claims = list(connection.execute("SELECT id, item, payload, state FROM publication_claim"))
    if not claims:
        return
    records = _journal_files(directory / "publications")
    for claim in claims:
        manifest = json.loads(records.get(f"{claim['id']}/manifest.json", b"{}"))
        states = [json.loads(content)["state"] for name, content in records.items()
                  if name.startswith(f"{claim['id']}/") and name != f"{claim['id']}/manifest.json"]
        if (manifest.get("item") != claim["item"] or manifest.get("payload") != json.loads(claim["payload"])
                or json.loads(claim["state"]) not in states):
            raise BackupError(f"publication claim {claim['id']} differs from the backup journal")


def _journal_union(snapshot: dict[str, bytes], live: dict[str, bytes]) -> dict[str, bytes]:
    result = dict(live)
    for name, content in snapshot.items():
        existing = result.get(name)
        if existing is not None and existing != content:
            if name != "claims.jsonl" or not (existing.startswith(content) or content.startswith(existing)):
                raise BackupError(f"publication journal conflicts with live evidence: {name}; preserve both copies")
            content = max((existing, content), key=len)
        result[name] = content
    return result


def _pending_publications(state: Path, saved: dict[str, bytes]) -> tuple[dict[str, bytes], Path] | None:
    intent = state / PUBLICATION_RESTORE_INTENT
    if not intent.exists() and not intent.is_symlink():
        return None
    try:
        if intent.is_symlink() or not intent.is_file() or intent.stat().st_size > 4096:
            raise ValueError("linked, missing, or oversized intent")
        value = json.loads(intent.read_bytes())
        relative = Path(value["material"])
        if (value["version"] != 1 or len(relative.parts) != 3
                or relative.parts[0] != "publication-recovery-evidence"
                or not relative.parts[1].startswith("restore-") or relative.parts[2] != "publications"
                or any((state / Path(*relative.parts[:index])).is_symlink() for index in range(1, 4))):
            raise ValueError("invalid material identity")
        material = state / relative
        files = _journal_files(material)
        if _files_fingerprint(files) != value["material_fingerprint"]:
            raise ValueError("restore material changed")
        if _files_fingerprint(saved) != value["source_fingerprint"]:
            raise ValueError("resume the backup matching the pending restore source")
        return files, material
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise BackupError(f"publication restore intent is unresolved: {error}; preserve its evidence") from error


def _partial_publications(journal: Path, intended: dict[str, bytes]) -> dict[str, bytes]:
    """Only the recorded restore target can prove an interrupted live prefix."""
    if not journal.exists() and not journal.is_symlink():
        return {}
    if journal.is_symlink() or not journal.is_dir():
        raise BackupError("pending publication restore has a linked or invalid journal directory")
    directories = {str(Path(name).parent) for name in intended} - {"."}
    found = {}
    for path in journal.rglob("*"):
        name = path.relative_to(journal).as_posix()
        if path.is_symlink() or (path.is_dir() and name not in directories):
            raise BackupError("pending publication restore contains unexpected live evidence")
        if path.is_dir():
            continue
        if not path.is_file() or name not in intended:
            raise BackupError("pending publication restore contains unexpected live evidence")
        data = path.read_bytes()
        if not intended[name].startswith(data):
            raise BackupError(f"pending publication restore conflicts with live evidence: {name}")
        found[name] = data
    return found


def _begin_publications_restore(saved: dict[str, bytes], combined: dict[str, bytes], state: Path) -> Path:
    evidence = state / "publication-recovery-evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    if evidence.is_symlink() or not evidence.is_dir():
        raise BackupError("publication recovery evidence directory is linked or invalid")
    material = evidence / f"restore-{uuid.uuid4().hex}" / "publications"
    for name, data in combined.items():
        _write_recovery_record(material / name, data)
    _journal_files(material)
    _sync_directory(material.parent)
    _sync_directory(evidence)
    _sync_directory(state)
    intent = {"version": 1, "material": material.relative_to(state).as_posix(),
              "material_fingerprint": _files_fingerprint(combined), "source_fingerprint": _files_fingerprint(saved)}
    prepared_intent = material.parent / "intent.json"
    _write_recovery_record(prepared_intent, json.dumps(intent, sort_keys=True).encode())
    # Hard-linking a complete fsynced file publishes the hold atomically and
    # refuses an existing intent; a killed JSON write cannot strand recovery.
    os.link(prepared_intent, state / PUBLICATION_RESTORE_INTENT)
    _sync_directory(state)
    return material


def _prepare_publications(directory: Path, state: Path, stage: Path) -> dict[str, bytes]:
    snapshot, current = directory / "publications", state / "publications"
    saved = _journal_files(snapshot) if snapshot.exists() or snapshot.is_symlink() else {}
    pending = _pending_publications(state, saved)
    if pending is not None:
        combined, _ = pending
        _partial_publications(current, combined)
    else:
        live = _journal_files(current) if current.exists() or current.is_symlink() else {}
        combined = _journal_union(saved, live)
    if combined:
        prepared = stage / "publications"
        for name, content in combined.items():
            path = prepared / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        _journal_files(prepared)
    return saved


def _restore_publications(saved: dict[str, bytes], state: Path, target: Path) -> None:
    """Only add proven evidence. Never roll journals back with the database."""
    if not saved:
        return
    # Publication writers hold the database transaction while extending the
    # journal. Keep their catalog append serial with this merge, then release
    # the lock before SQLite's separate backup connection installs the DB.
    guard = sqlite3.connect(target, isolation_level=None, timeout=0) if target.exists() else None
    try:
        if guard is not None:
            guard.execute("BEGIN IMMEDIATE")
        journal = state / "publications"
        pending = _pending_publications(state, saved)
        if pending is not None:
            combined, material = pending
            live = _partial_publications(journal, combined)
        else:
            live = _journal_files(journal) if journal.exists() or journal.is_symlink() else {}
            combined = _journal_union(saved, live)
            material = _begin_publications_restore(saved, combined, state)
        journal.mkdir(mode=0o700, exist_ok=True)
        for name, content in sorted(combined.items(), key=lambda entry: (
                entry[0] == "claims.jsonl", str(Path(entry[0]).parent), Path(entry[0]).name != "manifest.json", entry[0])):
            path = journal / name
            if name in live:
                if content == live[name]:
                    continue
                # Usually only the catalog grows. Under a verified restore
                # intent, a killed file copy can also leave a byte prefix.
                with path.open("ab") as stream:
                    stream.write(content[len(live[name]):])
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                _write_recovery_record(path, content)
            _sync_directory(path.parent)
        _journal_files(journal)
        _sync_directory(state)
        os.replace(state / PUBLICATION_RESTORE_INTENT, material.parent / "completed-intent.json")
        _sync_directory(material.parent)
        _sync_directory(state)
    finally:
        if guard is not None:
            guard.close()


def _runner_files(directory: Path) -> dict[str, bytes]:
    from .runner_journal import validate_path
    try:
        validate_path(directory)
        return {path.name: path.read_bytes() for path in directory.glob("*.json")}
    except (SdDbError, OSError, ValueError, TypeError) as error:
        raise BackupError(f"runner journal is not a verified recovery source: {error}") from error


def _copy_runner_journal(source: Path, directory: Path, expected: dict[str, int]) -> bool:
    journal = source.parent / "runner-journal"
    intent = source.parent / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        raise BackupError("runner journal restore is incomplete; resume its verified backup first")
    if not journal.exists() and not journal.is_symlink():
        if expected.get("runner_run", 0):
            raise BackupError("runner rows exist but their durable journal is missing")
        return False
    _runner_files(journal)
    try:
        shutil.copytree(journal, directory / "runner-journal", symlinks=True)
    except OSError as error:
        raise BackupError(f"runner journal was not copied: {error}") from error
    _runner_files(directory / "runner-journal")
    return True


def _check_runner_records(connection: sqlite3.Connection, directory: Path) -> None:
    # `runner_run` arrives in migration 005, so a read-only snapshot of a
    # version 1-4 database has no such table and nothing to check. Same
    # reason as `_check_publication_claims` above, and the same gate: the
    # version says whether the table is owed, its absence does not.
    if schema_version(connection) < RUNNER_RUN_VERSION:
        return
    if "runner_run" not in set(tables(connection)):
        raise BackupError(f"a version {schema_version(connection)} database has no runner_run table; "
                          f"migration {RUNNER_RUN_VERSION:03d} creates it, so the copy is malformed")
    rows = list(connection.execute("SELECT * FROM runner_run"))
    if not rows:
        return
    from .runner_journal import against, canonical
    files = _runner_files(directory / "runner-journal")
    for row in rows:
        # A record written before migration 014 names the repository by its
        # absolute path; the row names the key. Both sides keyed (sd:1447).
        # A row `repo remove` detached has no repo; its journal keeps it (sd:2581).
        record = against(json.loads(files.get(f"{row['id']}.json", b"{}" )).get("record", {}), dict(row))
        row = canonical(dict(row))
        identity = ("id", "assignment", "run", "repo", "branch", "work_path", "retained_path")
        if (any(record.get(name) != row[name] for name in identity)
                or record.get("journal_version", -1) < row["journal_version"]
                or record.get("journal_version") == row["journal_version"] and record != dict(row)):
            raise BackupError(f"runner row {row['id']} differs from the backup journal")


def _compatible_runner_records(saved: dict[str, bytes], live: dict[str, bytes]) -> None:
    from .runner_journal import canonical
    for name in set(saved) & set(live):
        old, current = (json.loads(data)["record"] for data in (saved[name], live[name]))
        # A journal written before migration 014 names the repository by its
        # absolute path and one written after by its `~/` key (sd:1439).
        # One written before migration 018 has no `detached_from` (sd:2581).
        if (any(old[key] != current[key] for key in ("id", "assignment", "run", "branch", "work_path", "retained_path"))
                or not (old["repo"] == current["repo"] or paths.same(old["repo"], current["repo"]))
                or old["journal_version"] == current["journal_version"] and canonical(old) != canonical(current)):
            raise BackupError(f"runner journal conflicts with live evidence: {name}; preserve both copies")


def _prepare_runner_journal(directory: Path, state: Path) -> dict[str, bytes]:
    source, target = directory / "runner-journal", state / "runner-journal"
    saved = _runner_files(source) if source.exists() or source.is_symlink() else {}
    live = _runner_files(target) if target.exists() or target.is_symlink() else {}
    _compatible_runner_records(saved, live)
    intent = state / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        _runner_restore_material(saved, state)
    return saved


def _runner_restore_material(saved: dict[str, bytes], state: Path) -> Path:
    intent = state / "runner-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        try:
            if intent.is_symlink() or not intent.is_file() or intent.stat().st_size > 4096:
                raise ValueError("linked, missing, or oversized intent")
            value = json.loads(intent.read_bytes())
            relative = Path(value["material"])
            if (value["version"] != 1 or len(relative.parts) != 3
                    or relative.parts[0] != "runner-recovery-evidence" or not relative.parts[1].startswith("restore-")
                    or relative.parts[2] != "runner-journal"
                    or any((state / Path(*relative.parts[:index])).is_symlink() for index in range(1, 4))):
                raise ValueError("invalid material identity")
            material = state / relative
            if _files_fingerprint(_runner_files(material)) != value["fingerprint"] or _files_fingerprint(saved) != value["fingerprint"]:
                raise ValueError("resume the unchanged backup matching the pending restore source")
            return material
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise BackupError(f"runner restore intent is unresolved: {error}; preserve its evidence") from error
    evidence = state / "runner-recovery-evidence"
    evidence.mkdir(mode=0o700, exist_ok=True)
    if evidence.is_symlink() or not evidence.is_dir():
        raise BackupError("runner recovery evidence directory is linked or invalid")
    material = evidence / f"restore-{uuid.uuid4().hex}" / "runner-journal"
    material.mkdir(mode=0o700, parents=True)
    for name, data in saved.items():
        _write_recovery_record(material / name, data)
    _runner_files(material)
    value = {"version": 1, "material": material.relative_to(state).as_posix(), "fingerprint": _files_fingerprint(saved)}
    prepared = material.parent / "intent.json"
    _write_recovery_record(prepared, json.dumps(value, sort_keys=True).encode())
    _sync_directory(material.parent)
    _sync_directory(evidence)
    _sync_directory(state)
    os.link(prepared, intent)
    _sync_directory(state)
    return material


def _drop_install_copy(install: Path, target: Path) -> None:
    """Remove a restore's install copy, which only repeats its material record.

    A crash before the link leaves it unlinked; a crash after the link leaves
    it as the live journal file's second name. Any other link is refused.
    """
    try:
        details = install.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(details.st_mode):
        raise BackupError(f"runner restore install copy is linked or invalid: {install.name}; preserve its evidence")
    if details.st_nlink != 1:
        current = target.lstat() if target.exists() or target.is_symlink() else None
        if current is None or details.st_nlink != 2 or (details.st_dev, details.st_ino) != (current.st_dev, current.st_ino):
            raise BackupError(f"runner restore install copy has a link outside the live journal: {install.name}; preserve its evidence")
    install.unlink()
    _sync_directory(install.parent)


def _restore_runner_journal(saved: dict[str, bytes], state: Path) -> None:
    """Restore missing run identities; existing process ownership always wins."""
    if not saved:
        return
    from .runner_journal import lock
    material = _runner_restore_material(saved, state)
    journal = state / "runner-journal"
    journal.mkdir(mode=0o700, exist_ok=True)
    for name, saved_record in saved.items():
        with lock(journal / Path(name).with_suffix(".lock"), blocking=False):
            target = journal / name
            # The install copy is this record's own inode, so the live file
            # keeps one link; the material copy stays as restore evidence.
            install = material.parent / f"install-{name}"
            _drop_install_copy(install, target)
            if target.exists() or target.is_symlink():
                _compatible_runner_records({name: saved_record}, _runner_files(journal))
                # The retry after a restore killed between `os.link` below and
                # its `_sync_directory(journal)` arrives here: the link is
                # visible and not yet durable, and `_drop_install_copy` fsynced
                # only the material directory. So the journal directory is
                # fsynced on this path too, before the run is recorded
                # complete -- otherwise the intent is replaced and the
                # restore reads as finished over a link a power loss removes.
                _sync_directory(journal)
                continue
            _write_recovery_record(install, saved_record)
            # Link the complete fsynced record atomically; never leave a
            # partially copied run record for startup to misinterpret.
            os.link(install, target)
            _sync_directory(journal)
            install.unlink()
            _sync_directory(material.parent)
    _runner_files(journal)
    _sync_directory(state)
    os.replace(state / "runner-restore-intent.json", material.parent / "completed-intent.json")
    _sync_directory(material.parent)
    _sync_directory(state)


TRANSIENT_INSTALL_RE = re.compile(r"install-[0-9a-f]{32}\.json")


def _transient_install(relative: Path) -> bool:
    """Whether an archive entry is a restore's in-flight install copy.

    Exactly `restore-*/install-<record>.json`, the one name
    `_restore_runner_journal` makes and unlinks again: the record is a run id,
    which is a `uuid4().hex`. A deeper entry, or an `install-` name of any
    other shape, is evidence and is preserved rather than dropped -- a
    corrupted `install-notes.txt` beside a restore is exactly the artifact the
    evidence check exists to keep.
    """
    parts = relative.parts
    return (len(parts) == 2 and parts[0].startswith("restore-")
            and TRANSIENT_INSTALL_RE.fullmatch(parts[1]) is not None)


def _ignore_transient_installs(directory: str, names: list[str]) -> set[str]:
    """`shutil.copytree`'s filter for the same entries, by the same shape."""
    if not Path(directory).name.startswith("restore-"):
        return set()
    return {name for name in names if TRANSIENT_INSTALL_RE.fullmatch(name)}


def _copy_recovery_evidence(source: Path, directory: Path) -> list[str]:
    """Preserve diagnostic archives without treating them as active journals.

    `install-<record>.json` under a `restore-*` directory is the only entry
    here that is not evidence. `_restore_runner_journal` creates it between
    `os.link` and `install.unlink()`, and a restore that starts after
    `_copy_runner_journal`'s intent check has already passed is inside this
    walk with one on disk. Left in, it either lands in the archive copy as a
    duplicate of a live record, or -- the loud half -- vanishes between the
    hash pass and the copy and fails the whole backup with "recovery archive
    changed during backup". Neither is a fault of the store, so the transient
    is left out of both passes and out of the copy.

    This does not make backup and restore mutually exclusive; they still
    share no lock, and `_copy_runner_journal`'s intent check is the only
    ordering between them. What it does is stop the one named transient from
    deciding whether the night's backup succeeds.
    """
    copied, hashes = [], {}
    for name in ("publication-recovery-evidence", "runner-recovery-evidence"):
        archive = source.parent / name
        if not archive.exists() and not archive.is_symlink():
            continue
        try:
            if archive.is_symlink() or not archive.is_dir():
                raise BackupError(f"recovery archive is linked or invalid: {name}")
            expected = {}
            for path in archive.rglob("*"):
                if _transient_install(path.relative_to(archive)):
                    continue
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise BackupError(f"recovery archive contains a linked or invalid entry: {name}")
                if path.is_file():
                    expected[path.relative_to(archive).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            target = directory / name
            shutil.copytree(archive, target, symlinks=True, ignore=_ignore_transient_installs)
            actual = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in target.rglob("*")
                      if path.is_file() and not path.is_symlink()
                      and not _transient_install(path.relative_to(target))}
            if actual != expected or any(path.is_symlink() for path in target.rglob("*")):
                raise BackupError(f"recovery archive changed during backup: {name}")
            hashes[name] = expected
            copied.append(name)
        except OSError as error:
            raise BackupError(f"recovery archive was not copied: {name}: {error}") from error
    if hashes:
        _write_recovery_record(directory / "recovery-evidence-sha256.json", json.dumps(hashes, sort_keys=True).encode())
    return copied


def backup_root(home: Path | str) -> Path:
    """`ROOT_VARIABLE` when it is set, under `home` if relative; else `DEFAULT_ROOT`."""
    named = os.environ.get(ROOT_VARIABLE)
    if named:
        return Path(home) / Path(named).expanduser()
    return DEFAULT_ROOT


@dataclass
class Snapshot:
    """One completed run: where it went, and what the check found."""

    directory: Path
    run_id: str
    counts: dict[str, int] = field(default_factory=dict)
    removed: list[Path] = field(default_factory=list)
    configuration: list[str] = field(default_factory=list)
    #: False when the source could not be written -- a database waiting for
    #: `migrate` -- so no checkpoint row proves the copy is this run's. Such a
    #: snapshot is verified by integrity and counts only, is never owned by
    #: retention, and runs no prune.
    checkpointed: bool = True
    #: What `PRAGMA foreign_key_check` found in the SOURCE, as its own rows:
    #: `(table, rowid, parent, fkid)`. Empty is the normal answer, and it is
    #: reported rather than raised -- see the call in `run`. Note that `fkid`
    #: is the index of the constraint within the child table and NOT a parent
    #: rowid; reading it as one produces a confident wrong claim about which
    #: row is orphaned, which is how sd:744 was misread the first time.
    violations: list[tuple] = field(default_factory=list)


def _dated_directory(root: Path, when: datetime) -> Path:
    """A directory named for the day, with a suffix if the day is taken.

    Two runs in a day happen -- a scheduled one and one before a migration --
    and the second must not overwrite the first, because the first is the one
    with the pre-migration rows.
    """
    stem = when.strftime("%Y-%m-%d")
    candidate = root / stem
    index = 1
    while candidate.exists():
        candidate = root / f"{stem}.{index}"
        index += 1
    return candidate


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    present = set(tables(connection))
    return {
        name: int(connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
        for name in TABLES
        if name in present
    }


def verify(copy: Path, *, run_id: str, expected: dict[str, int], checkpointed: bool = True) -> dict[str, int]:
    """Open the copy and prove it is the database that was just checked.

    Three questions, in the order that makes the answers useful: is the file
    a database at all, is it *this* run's database, and does it hold the same
    rows. A truncated copy fails the first or the third; a stale directory
    fails the second. A snapshot taken without a checkpoint row (`run` could
    not write the source) skips the second question and says so in its
    result; the manifest check never skips it, which is what keeps such a
    snapshot out of retention's hands.
    """
    if not copy.exists():
        raise BackupError(f"the snapshot at {copy} was not written")
    try:
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        raise BackupError(f"the snapshot at {copy} does not open: {error}") from None
    try:
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise BackupError(
                f"the snapshot at {copy} failed integrity_check: {error}"
            ) from None
        if result != "ok":
            raise BackupError(f"the snapshot at {copy} failed integrity_check: {result}")

        row = connection.execute(
            "SELECT id FROM state WHERE kind = 'checkpoint' AND key = ?", (run_id,)
        ).fetchone()
        if row is None and checkpointed:
            raise BackupError(
                f"the snapshot at {copy} holds no checkpoint row for run "
                f"{run_id}; it is a copy of some other moment"
            )

        found = _counts(connection)
        differing = {
            name: (expected[name], found.get(name))
            for name in expected
            if found.get(name) != expected[name]
        }
        if differing:
            detail = ", ".join(
                f"{name}: source {source}, snapshot {snapshot}"
                for name, (source, snapshot) in sorted(differing.items())
            )
            raise BackupError(f"the snapshot at {copy} has different counts -- {detail}")
        return found
    finally:
        connection.close()


def _retention_inventory(directory: Path) -> dict[str, str | None]:
    """Bind every entry, including empty directories, without following links."""
    inventory = {}
    device = directory.stat().st_dev
    for path in sorted(directory.rglob("*")):
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or details.st_dev != device:
            raise BackupError("backup retention refuses linked or mounted entries")
        if path == directory / BACKUP_MANIFEST:
            if not stat.S_ISREG(details.st_mode):
                raise BackupError("backup retention manifest is not a regular file")
            continue
        name = path.relative_to(directory).as_posix()
        if stat.S_ISREG(details.st_mode):
            inventory[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISDIR(details.st_mode):
            inventory[name] = None
        else:
            raise BackupError("backup retention refuses special files")
    return inventory


def _record_backup(directory: Path, source: Path, run_id: str, counts: dict[str, int], *,
                   checkpointed: bool = True) -> None:
    # `checkpoint: false` is a note for the reader, not a key `_owned_backup`
    # honours: it re-verifies through the checkpoint row, so a snapshot
    # without one is never owned, never pruned, and never counted for `--keep`.
    value = {"format": "sd-db-backup", "version": 1, "directory": str(directory.resolve()),
             "database": source.name, "run_id": run_id, "counts": counts,
             "checkpoint": checkpointed, "entries": _retention_inventory(directory)}
    _write_recovery_record(directory / BACKUP_MANIFEST, (json.dumps(value, sort_keys=True) + "\n").encode())


def _owned_backup(directory: Path, *, run_id: str | None = None) -> tuple[int, int, str] | None:
    """Legacy, moved, incomplete, modified, or unrelated directories stay put.

    `run_id` asks the stronger question `passed` needs: is this directory the
    backup *that run* took. Without it the answer is only "some owned backup",
    which is not the same claim once two runs have left a directory each.
    """
    try:
        if directory.is_symlink() or not directory.is_dir() or directory.parent.is_symlink():
            return None
        if not DATED_NAME.fullmatch(directory.name):
            return None
        datetime.strptime(directory.name[:10], "%Y-%m-%d")
        manifest = directory / BACKUP_MANIFEST
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 16 * 1024 * 1024:
            return None
        material = manifest.read_bytes()
        value = json.loads(material)
        database = value.get("database")
        counts = value.get("counts")
        stored = value.get("run_id")
        if run_id is not None and stored != run_id:
            return None
        run_id = stored
        if (value.get("format") != "sd-db-backup" or type(value.get("version")) is not int
                or value["version"] != 1 or value.get("directory") != str(directory.resolve())
                or not isinstance(database, str) or Path(database).name != database or database in ("", ".", "..")
                or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
                or not isinstance(counts, dict) or "state" not in counts
                or any(type(count) is not int or count < 0 for count in counts.values())
                or not isinstance(value.get("entries"), dict)
                or value["entries"] != _retention_inventory(directory)
                or database not in value["entries"] or value["entries"][database] is None):
            return None
        if verify(directory / database, run_id=run_id, expected=counts) != counts:
            return None
        details = directory.stat()
        return details.st_dev, details.st_ino, hashlib.sha256(material).hexdigest()
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error, BackupError):
        return None


def passed(connection: sqlite3.Connection, snapshot: Snapshot) -> bool:
    """Whether `snapshot` is a backup of this database that held up.

    Verified again, not taken on trust: the dated directory must still be an
    owned backup **of this run**, whose copy restores to its recorded counts,
    and its run's checkpoint row must be in the database asked. The nightly
    prune runs only on a `True` here (`retention.prune`).

    The run id binds the two halves together, and without that binding they
    answered about different runs: a directory that was some owned backup,
    and a checkpoint row that was some run's. With two valid runs on one
    machine, `Snapshot(first.directory, run_id=second.run_id)` then passed --
    the first directory is owned, the second run has its row -- and the prune
    ran against a snapshot that is not the one it names.
    """
    if not isinstance(snapshot, Snapshot) or not re.fullmatch(r"[0-9a-f]{32}", snapshot.run_id or ""):
        return False
    if _owned_backup(Path(snapshot.directory), run_id=snapshot.run_id) is None:
        return False
    row = connection.execute(
        "SELECT 1 FROM state WHERE kind = 'checkpoint' AND key = ?", (snapshot.run_id,)
    ).fetchone()
    return row is not None


def _check_keep(keep: int | None) -> None:
    if keep is not None and (type(keep) is not int or keep < 0):
        raise BackupError("backup retention needs a nonnegative count or no deletion")


def _check_keep_days(days: int | None) -> None:
    # Zero is refused rather than meaning "today only": a retention window is
    # the operator's promise of history, and an empty one is a typo.
    if days is not None and (type(days) is not int or days < 1):
        raise BackupError("backup retention by age needs a positive number of days")


def _directory_order(path: Path) -> tuple[str, int]:
    """The day, then the run within it as a number; a bare day is run 0."""
    return path.name[:10], int(path.name.partition(".")[2] or 0)


def _retention_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup retention root must be an existing unlinked directory")


def _remove_owned(directories: list[tuple[Path, tuple[int, int, str]]]) -> list[Path]:
    removed = []
    for path, identity in directories:
        if _owned_backup(path) != identity:
            raise BackupError(f"backup changed before retention: {path}; preserved for review")
        shutil.rmtree(path)
        removed.append(path)
    return removed


def prune(root: Path, keep: int | None = KEEP) -> list[Path]:
    """Apply requested retention only to unchanged, verified owned backups."""
    _check_keep(keep)
    if keep is None:
        return []
    _retention_root(root)
    directories = [(path, identity) for path in root.iterdir() if (identity := _owned_backup(path)) is not None]
    directories.sort(key=lambda value: _directory_order(value[0]))
    return _remove_owned(directories[:-keep] if keep else directories)


def prune_older(root: Path, days: int, *, now: datetime) -> list[Path]:
    """Delete owned backups taken more than `days` days before `now`.

    Age comes from the directory's day, which `_dated_directory` takes from
    the run's own clock, so it is compared in that clock's zone. A day is
    removed only when the whole of it lies more than `days` days back:
    `D < (now - days).date()`. Every removed snapshot is therefore older than
    the window, and none younger is touched; one dated exactly `days` days
    ago is kept, until the next day. So a window of 7 holds between 7 and 8
    days of runs. Counting runs instead (`--keep 168`) would hold a week
    only while every hourly run happens; a Mac asleep overnight makes 168
    runs reach further back, and one that ran twice an hour makes it fall
    short.

    Only a directory whose day is past the window is verified -- the cheap
    name test first -- so an hourly run opens the few snapshots it may
    delete, not the whole week. What it deletes is still only an unchanged,
    verified owned backup, exactly as `prune` requires.
    """
    _check_keep_days(days)
    _retention_root(root)
    cutoff = (now - timedelta(days=days)).date()
    directories = []
    for path in root.iterdir():
        if not DATED_NAME.fullmatch(path.name):
            continue
        try:
            day = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        if (identity := _owned_backup(path)) is not None:
            directories.append((path, identity))
    directories.sort(key=lambda value: _directory_order(value[0]))
    return _remove_owned(directories)


def _mount_point(path: Path) -> Path:
    """The mount serving `path`: the path itself if it is one, else an ancestor."""
    point = path.resolve()
    while point != point.parent and not os.path.ismount(point):
        point = point.parent
    return point


def require_mount(mount: Path, destination: Path,
                  remedy: str = "attach the drive and run the backup again") -> None:
    """Refuse unless `mount` is mounted and `destination` is written onto it.

    `/Volumes/local` with its disk detached is an ordinary directory on the
    boot disk, and `mkdir(parents=True)` would recreate it there: the
    snapshot would then restore and compare perfectly on the disk it was
    meant to leave. So the mount is asked first, and then where the
    destination -- or the nearest part of it that exists, which is where the
    writer will create the rest -- really lands, links resolved. Nothing is
    written before this passes: no checkpoint row and no directory.
    """
    mount = Path(mount).expanduser()
    if not os.path.ismount(mount):
        raise BackupError(
            f"{mount} is not a mounted volume, so {destination} would be written onto this "
            f"machine's own disk; {remedy}"
        )
    existing = Path(destination).expanduser()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    served = _mount_point(existing)
    if served != mount.resolve():
        raise BackupError(
            f"{destination} is served by {served}, not by the mounted volume {mount}; "
            "refusing to write the backup there"
        )


def probe_destination(root: Path) -> None:
    """Refuse unless a test write under `root` finishes within `PROBE_SECONDS`.

    On some nights macOS stops answering permission checks for launchd jobs.
    Each open then blocks for seconds and fails with EINTR, Python retries it
    (PEP 475), and the backup ran until the job's two-hour limit killed it
    with nothing said (sd:2660). The write runs in a child, so an open that
    never returns holds the child and not this process. A probe that fails at
    once is not this check's to name: the writes below fail the same way and
    say which step did.
    """
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PROBE, str(root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise BackupError(f"the probe of backup destination {root} did not start: {error}") from None
    try:
        child.wait(timeout=PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise BackupError(
            f"backup destination {root} did not answer within {PROBE_SECONDS:g} s"
        ) from None


def run(
    *,
    home: Path | str,
    database: Path | str | None = None,
    destination: Path | str | None = None,
    keep: int | None = None,
    keep_days: int | None = None,
    mount: Path | str | None = None,
    when: datetime | None = None,
) -> Snapshot:
    """Take a snapshot, restore it, and compare. Raises on anything else.

    `keep` retains that many owned backups; `keep_days` retains owned backups
    by age (`prune_older`). They are two answers to one question, so asking
    both is refused. `mount` names the volume the destination must be on,
    checked before anything is written (`require_mount`). The destination
    must then finish a test write in time (`probe_destination`).
    """
    _check_keep(keep)
    if keep == 0:
        # The snapshot this run takes is one of the `keep` it retains, so 0
        # deletes it and the row prune then refuses a backup that is gone.
        raise BackupError("backup retention keeps at least 1: the count includes this run's backup")
    _check_keep_days(keep_days)
    if keep is not None and keep_days is not None:
        raise BackupError("backup retention takes a count or an age, not both")
    home = Path(home)
    # Before the mount check and the probe: a satellite writes nothing.
    refuse_hub_only(database if database is not None else local_path(home), "the backup", home)
    when = when or datetime.now(UTC)
    root = (
        Path(destination).expanduser()
        if destination is not None
        else backup_root(home)
    )
    if mount is not None:
        require_mount(Path(mount), root)
    elif destination is None and not os.environ.get(ROOT_VARIABLE):
        # The default root is on a disk that may be detached. Falling back to
        # the home would hide that, so a run names the fix and writes nothing.
        require_mount(DEFAULT_MOUNT, root, "mount the disk or pass --destination PATH")
    probe_destination(root)
    run_id = uuid.uuid4().hex

    checkpointed = True
    try:
        try:
            connection = connect(database, home=home)
        except SchemaTooOld:
            # A database waiting for `migrate` is exactly the one to snapshot
            # first, and the writable open is what refuses it. Read-only,
            # `mode=ro` keeps every write off the source; `query_only` would
            # also refuse the output file `VACUUM INTO` creates, so it is
            # lifted for this connection alone. The checkpoint row is a write,
            # so it is not taken and the copy is proved by counts only.
            connection = connect(database, home=home, write=False)
            connection.execute("PRAGMA query_only = 0")
            checkpointed = False
    except (OSError, sqlite3.Error, SdDbError, ValueError) as error:
        raise BackupError(f"the database did not open: {error}") from None

    try:
        source = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        if checkpointed:
            try:
                record_state(connection, "checkpoint", key=run_id, body=when.isoformat())
            except sqlite3.Error as error:
                raise BackupError(f"the checkpoint row was not written: {error}") from None
        expected = _counts(connection)
        try:
            root.mkdir(parents=True, exist_ok=True)
            directory = _dated_directory(root, when)
            directory.mkdir()
        except OSError as error:
            raise BackupError(f"the backup directory was not created: {error}") from None

        copy = directory / source.name
        try:
            connection.execute("VACUUM INTO ?", (str(copy),))
        except sqlite3.Error as error:
            # A full volume arrives here, as `database or disk is full`.
            raise BackupError(f"the snapshot was not written to {copy}: {error}") from None
    finally:
        connection.close()

    copied = []
    if _copy_publications(source, directory, expected):
        copied.append("publications")
    if _copy_runner_journal(source, directory, expected):
        copied.append("runner-journal")
    copied.extend(_copy_recovery_evidence(source, directory))
    for name in CONFIGURATION:
        beside = home / ".local/share/sd" / name
        if not beside.exists():
            continue
        try:
            shutil.copy2(beside, directory / name)
        except OSError as error:
            raise BackupError(f"{name} was not copied beside the snapshot: {error}") from None
        copied.append(name)

    from .runner_exec_backup import capture as capture_executions
    if capture_executions(source, directory):
        copied.extend(("executions", "execution-evidence.json"))

    counts = verify(copy, run_id=run_id, expected=expected, checkpointed=checkpointed)
    checked = connect(copy, write=False)
    try:
        # The referential check, and it runs on the COPY. The first reason is
        # correctness and not taste: `connect` opens with `isolation_level=
        # None`, so no read transaction spans the source connection and the
        # `VACUUM INTO` above. A check on the source would answer for an image
        # that is not the one on disk here -- a writer landing between the two
        # turns a valid row into an orphan the check never saw, and the table
        # counts `verify` compares stay equal while it happens. The copy
        # cannot move. The second reason is that the copy is the image
        # `restore` would consume, so it is the one the answer is about.
        #
        # `verify` runs `integrity_check` on this same copy, but that pragma
        # asks whether the b-tree pages are well formed, not whether rows
        # point at rows that exist -- a database of nothing but orphans passes
        # it, and `VACUUM INTO` copies orphans faithfully. Measured: sd:744's
        # six orphan rows survived the nightly backups of 2026-09-11, -12 and
        # -13, each of which reported "restored and compared". The check the
        # outbound path was missing already existed in this file, at
        # `_check_restore`, guarding the inbound one.
        #
        # It reports and does not raise, which is the deliberate half: a store
        # that has just gone referentially bad is the one whose bytes are most
        # worth keeping, and refusing to copy it destroys the only record of
        # what it looked like before somebody repaired it. The count reaches
        # the operator through the job's summary line instead.
        #
        # WHAT THAT SNAPSHOT IS, STATED EXACTLY, because the first version of
        # this comment called it "the recovery path" and that was wrong.
        # `restore` refuses it. `_check_restore` below runs the same pragma on
        # the candidate and raises at the `foreign_key_check` arm, so a
        # snapshot of a broken source is a FORENSIC copy and not a restorable
        # one, and it stays that way until the source is repaired and a later
        # night copies a clean store. `test_a_snapshot_of_a_broken_source_is_not
        # _restorable` pins that, so the contract is tested and not merely
        # asserted here. The value of taking it anyway is the pre-repair
        # record: the orphans are queryable in the copy after somebody deletes
        # them from the live database, which is how sd:744 was reconstructed.
        try:
            violations = [tuple(row) for row in
                          checked.execute("PRAGMA foreign_key_check")]
        except sqlite3.Error as error:
            # A foreign-key MISMATCH -- a constraint naming a column that is
            # not a key -- raises here rather than returning rows. That is a
            # schema fault, not a data one, and it is not something a summary
            # line can carry, so it takes the module's documented exit.
            raise BackupError(f"the referential check did not run: {error}") from None
        _check_publication_claims(checked, directory)
        _check_runner_records(checked, directory)
    finally:
        checked.close()

    try:
        _record_backup(directory, source, run_id, counts, checkpointed=checkpointed)
    except OSError as error:
        raise BackupError(f"backup ownership was not recorded: {error}") from None
    try:
        if keep_days is not None:
            removed = prune_older(root, keep_days, now=when)
        else:
            removed = prune(root, keep) if keep is not None else []
    except OSError as error:
        raise BackupError(f"old snapshots were not removed: {error}") from None

    return Snapshot(
        directory=directory,
        run_id=run_id,
        counts=counts,
        removed=removed,
        configuration=copied,
        checkpointed=checkpointed,
        violations=violations,
    )


def _schema_objects(connection: sqlite3.Connection, table_names: list[str]) -> dict[tuple[str, str], tuple]:
    """Each table's indexes by shape (unique, origin, partial, key columns),
    and every trigger and view by the table it is on."""
    shape: dict[tuple[str, str], tuple] = {}
    for table in table_names:
        for _, name, unique, origin, partial in connection.execute(f"PRAGMA index_list({table})"):
            columns = tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{name}")'))
            shape[("index", name)] = (table, unique, origin, partial, columns)
    for kind, name, table in connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master WHERE type IN ('trigger', 'view')"
    ):
        shape[(kind, name)] = (table,)
    return shape


def _check_restore(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> dict[str, int]:
    """Check the complete candidate, including a misleading version number."""
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"restore candidate failed integrity_check: {result}")
    if schema_version(connection) != version or not 1 <= version <= SCHEMA_VERSION:
        raise BackupError("restore candidate claims an unsupported schema version")
    reference = sqlite3.connect(":memory:")
    try:
        # 014 calls the functions `migrate` registers; without them the
        # reference fails with "no such function" before comparing anything.
        paths.install(reference)
        for number, path in migrations():
            if number <= version:
                reference.executescript(path.read_text(encoding="utf-8"))
        expected_tables = tables(reference)
        if set(tables(connection)) != set(expected_tables):
            raise BackupError("restore candidate has an incompatible table set")
        for table in expected_tables:
            actual = [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
            expected = reference.execute(f"PRAGMA table_info({table})").fetchall()
            if actual != expected:
                raise BackupError(f"restore candidate has incompatible columns in {table}")
        # Tables and columns alone pass a store missing an index or a trigger:
        # `record_check` upserts through `runner_check`, and a trigger holds
        # the publication payload immutable.
        wanted, found = _schema_objects(reference, expected_tables), _schema_objects(connection, expected_tables)
        changed = sorted(name for kind, name in wanted.keys() | found.keys()
                         if wanted.get((kind, name)) != found.get((kind, name)))
        if changed:
            raise BackupError("restore candidate has incompatible indexes or triggers: " + ", ".join(changed))
    finally:
        reference.close()
    # A snapshot taken by `run` of a source that was already broken arrives
    # here, because `run` reports those violations and copies the store
    # anyway. Refusing is right -- restoring known-orphaned rows over a live
    # database is not a recovery -- and the message names the count and the
    # tables, or the operator cannot tell what it is looking at.
    #
    # It does NOT say the source was broken when the snapshot was taken, which
    # an earlier version did. This check sees the candidate and nothing else:
    # the same rows would appear if the file was damaged in storage or in
    # transit afterwards. Nothing in the directory records the finding `run`
    # made, so that history is unavailable here and the message stays inside
    # what was actually observed, naming the forensic case as the likely one.
    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        where = ", ".join(sorted({str(row[0]) for row in broken}))
        raise BackupError(
            f"restore candidate has {len(broken)} foreign key violation(s), in {where}, "
            f"so it is not a restore point; `run` copies a broken source deliberately, "
            f"so a forensic snapshot is the usual cause, but this check observes only "
            f"the candidate and cannot tell that from later damage"
        )
    return _counts(connection)


@dataclass(frozen=True)
class RestoreCheck:
    """What `restore`'s validator makes of one database file.

    `refusal` is `None` exactly when the file is a restore point, and carries
    the validator's own words otherwise -- a file that will not open included,
    because a backup that cannot be opened is refused by any other name.
    `counts` is what the caller compares against whatever manifest it holds;
    this module does not know what that manifest says.
    """

    counts: dict[str, int]
    version: int | None
    refusal: str | None

    @property
    def accepted(self) -> bool:
        return self.refusal is None


def check_restorable(database: Path | str) -> RestoreCheck:
    """Ask `restore`'s own question of one database file, and report the answer.

    `restore` stages a whole snapshot directory. A caller holding the database
    alone -- `local-mirror-sync/offsite-verify.py`, which pulls one back off
    the share -- asks the same question of the file, and asks it here because
    nothing outside this library opens the database. Keeping a second, weaker
    contract out there is the failure an unverified backup is made of: a
    verifier that passes what `restore` would refuse reports a good backup for
    a snapshot nobody can restore.

    Nothing is raised. Every refusal is an outcome this caller prints beside
    the checks it ran, not an exception that would end the run before the
    remaining ones report.

    The version is the file's own. `restore` validates a snapshot against the
    shape it claims and migrates a staged copy forward, so checking against
    today's schema instead would refuse every snapshot taken before the last
    migration -- which is most of what a share holds. A file claiming a
    version this build does not have reaches the validator against this
    build's, and is refused there for claiming it.
    """
    path = Path(database)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        return RestoreCheck({}, None, f"the file does not open as a database: {error}")
    version: int | None = None
    try:
        try:
            version = schema_version(connection)
            counts = (_check_restore(connection, version=version) if version < SCHEMA_VERSION
                      else _check_restore(connection))
        except (BackupError, sqlite3.Error) as error:
            return RestoreCheck({}, version, str(error))
        return RestoreCheck(counts, version, None)
    finally:
        connection.close()


def _upgrade_restore_candidate(candidate: Path) -> None:
    """Validate the claimed old shape before migrating only the staged copy."""
    connection = connect(candidate, write=False)
    try:
        version = schema_version(connection)
        before = (_check_restore(connection, version=version) if version < SCHEMA_VERSION
                  else _check_restore(connection))
    finally:
        connection.close()
    if version == SCHEMA_VERSION:
        return
    migrate_database(candidate)
    connection = connect(candidate, write=False)
    try:
        after = _check_restore(connection)
        if any(after.get(table) != count for table, count in before.items()):
            raise BackupError("restore migration changed existing table counts; keep the original snapshot and inspect the migration")
    finally:
        connection.close()


class _InstalledButUnverified(BackupError):
    """The SQLite commit landed but its post-check could not be completed."""


def _install_restore(candidate: Path, target: Path, expected: dict[str, int]) -> None:
    """SQLite owns the destination and its WAL; never replace or unlink them.

    Its backup API holds one destination transaction and rolls it back when
    copying aborts before SQLITE_DONE. Existing connections therefore see a
    coherent replacement, and failure cannot strand committed WAL data.
    https://sqlite.org/c3ref/backup_finish.html
    """
    source = connect(candidate, write=False)
    try:
        destination = sqlite3.connect(target, isolation_level=None, timeout=0)
        destination.row_factory = sqlite3.Row
        try:
            deadline = time.monotonic() + RESTORE_SECONDS

            def progress(status: int, remaining: int, total: int) -> None:
                # SQLITE_DONE has already committed; never report a timeout
                # after success and imply the old database was retained.
                if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
                    raise BackupError("restore timed out; stop database writers and retry")

            source.backup(destination, pages=128, progress=progress, sleep=0.05)
            try:
                if _check_restore(destination) != expected:
                    raise BackupError("restored table counts differ from the validated candidate")
            except (BackupError, sqlite3.Error, OSError) as error:
                raise _InstalledButUnverified(
                    f"restore was installed but its verification failed: {error}; "
                    "keep services stopped and inspect the database"
                ) from error
        finally:
            destination.close()
    finally:
        source.close()


def restore(directory: Path | str, *, home: Path | str) -> Path:
    """Put a dated directory's database and configuration back in place.

    A restored database is a record, not a permission: this writes the
    `restore` row and leaves it unresolved. While it is unresolved the runner
    dispatches nothing and the palette runs nothing -- enforced by the
    callers that read it, which is the only place that can enforce it.
    """
    from .operations import control_gate

    directory = Path(directory)
    home = Path(home)
    refuse_hub_only(local_path(home), "the restore", home)
    snapshot = directory / "sd.db"
    if not snapshot.exists():
        raise BackupError(f"{directory} holds no sd.db")
    # Backups produced by run() are closed single-file VACUUM snapshots. A
    # nonempty WAL means somebody handed us a live/copy-in-progress database,
    # whose main file alone is not the snapshot they asked to restore.
    wal = snapshot.with_name(snapshot.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise BackupError("restore source has a nonempty WAL; take a completed backup first")
    state = home / ".local/share/sd"
    target = state / "sd.db"
    stage = Path(tempfile.mkdtemp(prefix="sd-restore-"))
    keep_stage = False
    try:
        candidate = stage / "sd.db"
        shutil.copy2(snapshot, candidate)
        _upgrade_restore_candidate(candidate)
        publications = _prepare_publications(directory, state, stage)
        runner_journal = _prepare_runner_journal(directory, state)
        connection = connect(candidate)
        try:
            _check_publication_claims(connection, directory)
            _check_runner_records(connection, directory)
            from .runner_exec_backup import prepare as prepare_executions
            executions = prepare_executions(directory, state, connection)
            from .recovery import prepare_restore
            prepare_restore(connection)
            record_state(connection, "restore", key=directory.name,
                         body={"from": str(directory), "restored_at": datetime.now(UTC).isoformat()})
            expected = _check_restore(connection)
        finally:
            connection.close()

        configuration = []
        for name in CONFIGURATION:
            beside = directory / name
            if not beside.exists():
                continue
            current = state / name
            if beside.is_symlink() or current.is_symlink():
                raise BackupError(f"restore refuses a configuration symlink: {name}")
            prepared = stage / f"new-{name}"
            shutil.copy2(beside, prepared)
            original = stage / f"old-{name}" if current.exists() else None
            if original is not None:
                shutil.copy2(current, original)
            configuration.append((current, prepared, original))

        # Validation above is read-only for the live state. Hold the same gate
        # as launchctl controls throughout live journal/config/database changes.
        with control_gate(target):
            for current, _, original in configuration:
                changed = current.is_symlink()
                if original is None:
                    changed = changed or current.exists()
                else:
                    changed = changed or not current.is_file() or current.read_bytes() != original.read_bytes()
                    if not changed:
                        changed = current.stat().st_mode != original.stat().st_mode
                if changed:
                    raise BackupError(f"configuration changed while staging restore: {current.name}; retry from current state")
            state.mkdir(parents=True, exist_ok=True)
            _restore_publications(publications, state, target)
            _restore_runner_journal(runner_journal, state)
            from .runner_exec_backup import install as install_executions
            install_executions(executions, state)
            installed = []
            try:
                for current, prepared, original in configuration:
                    # Stage may be on a different filesystem. Copy beside the
                    # destination before the atomic replacement.
                    local = state / f".restore-{uuid.uuid4().hex}"
                    try:
                        shutil.copy2(prepared, local)
                        os.replace(local, current)
                    finally:
                        local.unlink(missing_ok=True)
                    installed.append((current, original))
                _install_restore(candidate, target, expected)
            except _InstalledButUnverified:
                # The DB commit succeeded. Keep matching new configuration and
                # report the boundary honestly rather than roll back half of it.
                raise
            except BaseException as failure:
                try:
                    for current, original in reversed(installed):
                        if original is None:
                            current.unlink()
                        else:
                            shutil.copy2(original, current)
                except OSError as rollback:
                    keep_stage = True
                    raise BackupError(
                        f"restore failed ({failure}); configuration rollback failed ({rollback}); "
                        f"original copies retained at {stage}"
                    ) from rollback
                raise
    except (sqlite3.Error, OSError, SdDbError, ValueError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"restore refused before completion: {error}") from error
    finally:
        if not keep_stage:
            shutil.rmtree(stage)
    return target
