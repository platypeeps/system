"""Write-ahead external-operation evidence that a database restore never rolls back."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from . import paths
from .database import refuse_hub_only
from .workflow import WorkflowError


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def root(connection):
    refuse_hub_only(connection, "the publication journal")
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        raise WorkflowError("publication needs a file-backed database and durable journal")
    return Path(database).parent / "publications"


def assert_restore_complete(connection):
    intent = root(connection).parent / "publication-restore-intent.json"
    if intent.exists() or intent.is_symlink():
        raise WorkflowError("publication journal restore is incomplete; resume the recorded restore before publication or prefix repair")


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise WorkflowError("publication journal has a missing, linked, or oversized record")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise WorkflowError("publication journal is unreadable or corrupt; restore its verified backup") from error


def initialise(connection):
    assert_restore_complete(connection)
    path = root(connection)
    restored = connection.execute("SELECT 1 FROM state WHERE kind='restore' LIMIT 1").fetchone()
    if not path.exists():
        if restored:
            raise WorkflowError("publication journal is missing after restore; recover it before any publication")
        path.mkdir(mode=0o700)
        _sync_directory(path.parent)
        _write_new(path / "format.json", _bytes({"version": 1}))
        _write_new(path / "claims.jsonl", b"")
    if path.is_symlink() or _read(path / "format.json") != {"version": 1}:
        raise WorkflowError("publication journal format is missing or unsupported")
    return path


def read(connection, claim):
    path = initialise(connection) / claim
    return _read_claim(path)


def _read_claim(path):
    manifest = _read(path / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("payload"), dict) or manifest.get("claim") != path.name or manifest.get("payload_sha256") != _hash(_bytes(manifest.get("payload"))):
        raise WorkflowError("publication journal manifest identity or payload hash is corrupt")
    events = sorted(path.glob("[0-9]*.json"))
    if {entry.name for entry in path.iterdir()} != {"manifest.json", *(event.name for event in events)}:
        raise WorkflowError("publication journal contains an unexpected claim file")
    if not events:
        raise WorkflowError("publication journal has no durable state event")
    previous = _hash(_bytes(manifest))
    state = None
    for sequence, source in enumerate(events, 1):
        if source.name != f"{sequence:08d}.json":
            raise WorkflowError("publication journal sequence is incomplete")
        event = _read(source)
        if not isinstance(event, dict) or event.get("previous_sha256") != previous or not isinstance(event.get("state"), dict):
            raise WorkflowError("publication journal hash chain is corrupt")
        previous = _hash(_bytes(event))
        state = event["state"]
    return manifest, state, len(events), previous


def create(connection, claim, item, payload, state):
    path = initialise(connection) / claim
    path.mkdir(mode=0o700)
    _sync_directory(path.parent)
    manifest = {"claim": claim, "item": item, "payload": payload,
                "payload_sha256": _hash(_bytes(payload))}
    _write_new(path / "manifest.json", _bytes(manifest))
    _write_new(path / "00000001.json", _bytes({"previous_sha256": _hash(_bytes(manifest)), "state": state}))
    catalog = path.parent / "claims.jsonl"
    if catalog.is_symlink() or not catalog.is_file():
        raise WorkflowError("publication journal catalog is missing")
    data = catalog.read_bytes()
    entry = {"claim": claim, "manifest_sha256": _hash(_bytes(manifest)), "previous_sha256": _hash(data)}
    with catalog.open("ab") as stream:
        stream.write(_bytes(entry) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def append(connection, claim, state):
    _, previous_state, count, digest = read(connection, claim)
    if previous_state == state:
        return
    _write_new(root(connection) / claim / f"{count + 1:08d}.json",
               _bytes({"previous_sha256": digest, "state": state}))


def require_synced(connection, claim, payload, state):
    manifest, recorded, _, _ = read(connection, claim)
    if manifest.get("payload_sha256") != _hash(_bytes(payload)) or manifest.get("payload") != payload:
        raise WorkflowError("database publication differs from its immutable external journal")
    if recorded != state:
        raise WorkflowError("publication journal is ahead of the database; recover the journal before dispatch")


def for_piece(connection, repo, piece):
    """Every journalled claim for this piece of this repository.

    A manifest written before migration 014 names the repository by its
    absolute path; `paths.same_key` matches it to the keyed row, so the claim
    guard and recovery still see it (sd:1450).
    """
    inventory = validate_path(initialise(connection))
    return [(entry["manifest"], entry["state"]) for entry in inventory.values()
            if entry["manifest"]["payload"].get("piece") == piece
            and paths.same_key(entry["manifest"]["payload"].get("repo"), repo)]


def validate_path(path):
    """Validate a complete journal without creating or changing any file.

    Catalog bytes and each event sequence are append-only. A prior backup is
    compatible only when its catalog and each existing claim's events are exact
    prefixes of live evidence; a restore must preserve the newer live suffix.
    """
    inventory = {}
    base = Path(path)
    if base.is_symlink() or not base.is_dir() or _read(base / "format.json") != {"version": 1}:
        raise WorkflowError("publication journal is missing or unsupported")
    catalog = base / "claims.jsonl"
    if catalog.is_symlink() or not catalog.is_file():
        raise WorkflowError("publication journal catalog is missing")
    entries, prefix = {}, b""
    try:
        for line in catalog.read_bytes().splitlines(keepends=True):
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("claim"), str) or not line.endswith(b"\n") or entry["previous_sha256"] != _hash(prefix) or entry["claim"] in entries:
                raise ValueError
            entries[entry["claim"]] = entry["manifest_sha256"]
            prefix += line
    except (ValueError, KeyError, TypeError) as error:
        raise WorkflowError("publication journal catalog is corrupt") from error
    directories = {path.name for path in base.iterdir() if path.is_dir()}
    if directories != set(entries):
        raise WorkflowError("publication journal has missing or unregistered claim evidence")
    for claim_path in sorted(base.iterdir()):
        if claim_path.name in {"format.json", "claims.jsonl"}:
            continue
        if claim_path.is_symlink() or not claim_path.is_dir() or len(claim_path.name) != 32 or any(char not in "0123456789abcdef" for char in claim_path.name):
            raise WorkflowError("publication journal contains an unexpected entry")
        manifest, state, count, digest = _read_claim(claim_path)
        if _hash(_bytes(manifest)) != entries[claim_path.name]:
            raise WorkflowError("publication journal manifest differs from its catalog")
        inventory[claim_path.name] = {"manifest": manifest, "state": state, "event_count": count, "last_sha256": digest}
    return inventory


def repair_incomplete(connection):
    """Explicitly preserve and retire creation prefixes that could not dispatch.

    A registered catalog entry, database claim, second state event, or restore
    history rules out this repair. Such evidence needs normal reconciliation.
    All incomplete bytes are moved into a separate recovery-evidence directory;
    nothing is deleted and no uncertain external operation is declared absent.
    """
    assert_restore_complete(connection)
    base = root(connection)
    if not base.exists():
        initialise(connection)
        return {"archived": []}
    if base.is_symlink() or not base.is_dir():
        raise WorkflowError("publication journal is not a regular directory")
    if connection.execute("SELECT 1 FROM state WHERE kind='restore' LIMIT 1").fetchone():
        raise WorkflowError("incomplete publication evidence after restore cannot be dismissed as pre-dispatch")

    def archive(source):
        parent = base.parent / "publication-recovery-evidence"
        parent.mkdir(mode=0o700, exist_ok=True)
        destination = parent / (uuid.uuid4().hex + "-" + source.name)
        source.rename(destination)
        _sync_directory(parent)
        _sync_directory(source.parent)
        return str(destination)

    children = list(base.iterdir())
    if any(path.is_symlink() for path in children):
        raise WorkflowError("linked publication evidence cannot be repaired automatically")
    claims_exist = connection.execute("SELECT 1 FROM publication_claim LIMIT 1").fetchone()
    if not any(path.is_dir() for path in children) and not claims_exist:
        allowed = {"format.json", "claims.jsonl"}
        if any(path.name not in allowed for path in children):
            raise WorkflowError("unexpected publication initialization evidence")
        catalog = base / "claims.jsonl"
        if catalog.exists() and catalog.read_bytes():
            raise WorkflowError("nonempty publication catalog needs claim reconciliation")
        if (base / "format.json").exists() and (base / "format.json").read_bytes() == _bytes({"version": 1}) and catalog.exists():
            validate_path(base)
            return {"archived": []}
        preserved = archive(base)
        initialise(connection)
        return {"archived": [preserved]}
    if _read(base / "format.json") != {"version": 1}:
        raise WorkflowError("publication format is corrupt beyond an initialization prefix")
    catalog = base / "claims.jsonl"
    if not catalog.is_file():
        raise WorkflowError("missing publication catalog with claim evidence requires backup recovery")
    data, prefix, known = catalog.read_bytes(), b"", {}
    for line in data.splitlines(keepends=True):
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("claim"), str) or not isinstance(entry.get("manifest_sha256"), str) or not line.endswith(b"\n") or entry["previous_sha256"] != _hash(prefix) or entry["claim"] in known:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            break
        known[entry["claim"]] = entry["manifest_sha256"]
        prefix += line
    orphaned = [path for path in children if path.is_dir() and path.name not in known]
    if data != prefix and not orphaned:
        raise WorkflowError("corrupt catalog has no provably undispatched creation to repair")
    for path in orphaned:
        if len(path.name) != 32 or any(char not in "0123456789abcdef" for char in path.name):
            raise WorkflowError("unexpected publication evidence directory")
        if connection.execute("SELECT 1 FROM publication_claim WHERE id=?", (path.name,)).fetchone():
            raise WorkflowError("database claim exists for incomplete journal; restore evidence instead")
        names = {entry.name for entry in path.iterdir()}
        if names - {"manifest.json", "00000001.json"}:
            raise WorkflowError("publication may have dispatched; incomplete evidence cannot be retired")
        first = path / "00000001.json"
        if first.exists():
            try:
                event = json.loads(first.read_text())
            except ValueError:
                event = None
            if event is not None and (not isinstance(event, dict) or not isinstance(event.get("state"), dict) or event["state"].get("phase") != "claimed" or event["state"].get("pending") or event["state"].get("document_id")):
                raise WorkflowError("publication may have dispatched; retain its uncertain evidence")
    # Validate the registered prefix before moving any incomplete evidence.
    for claim, manifest_sha256 in known.items():
        manifest, _, _, _ = _read_claim(base / claim)
        if _hash(_bytes(manifest)) != manifest_sha256:
            raise WorkflowError("publication journal manifest differs from its catalog")
    archived = [archive(path) for path in orphaned]
    if data != prefix:
        archived.append(archive(catalog))
        _write_new(catalog, prefix)
    validate_path(base)
    return {"archived": archived}
