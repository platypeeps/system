"""Replayable whole-clone restore with a durable destination-specific intent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path

from sd_db.runner import RunnerRefused
from sd_db.runner_journal import fsync_directory, lock


def inventory(path: Path, *, skip=None) -> dict:
    """Every entry under `path` with its digest; `skip` prunes as in `storage.walk`."""
    result = {}
    for root, directories, files in os.walk(path, followlinks=False):
        if skip is not None:
            base = Path(root).relative_to(path)
            directories[:] = [name for name in directories if not skip(base / name)]
            files = [name for name in files if not skip(base / name)]
        for name in sorted(directories + files):
            candidate = Path(root) / name
            relative = str(candidate.relative_to(path))
            details = candidate.lstat()
            entry = {"mode": stat.S_IMODE(details.st_mode)}
            if candidate.is_symlink():
                entry = {"link": os.readlink(candidate)}
            elif candidate.is_dir():
                entry["directory"] = True
            elif candidate.is_file():
                with candidate.open("rb") as stream:
                    entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            else:
                raise RunnerRefused(f"restore cannot represent special file {relative}")
            result[relative] = entry
    return result


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _write(path: Path, value: dict):
    temporary = path.with_suffix(".partial")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def _archive_members(source: tarfile.TarFile) -> dict:
    members = {}
    for member in source.getmembers():
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "clone":
            raise RunnerRefused("archive contains a path outside its clone")
        if len(path.parts) == 1:
            if not member.isdir():
                raise RunnerRefused("archive clone root is not a directory")
            continue
        relative = str(Path(*path.parts[1:]))
        if relative in members or not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise RunnerRefused("archive contains duplicate or special entries")
        members[relative] = member
    for name in members:
        if any(str(parent) in members and not members[str(parent)].isdir() for parent in Path(name).parents):
            raise RunnerRefused("archive path traverses a file or symbolic link")
    return members


def status(run: dict, destination: Path) -> dict:
    """Observe a restore intent without retrying or writing any restore step."""
    destination = destination.absolute()
    token = hashlib.sha256(str(destination).encode()).hexdigest()[:24]
    intent = destination.parent / f".sd-restore-{token}.json"
    if not intent.exists():
        return {"state": "unobserved", "run": run["id"], "destination": str(destination)}
    if intent.is_symlink() or destination.is_symlink():
        raise RunnerRefused("restore receipt or destination is linked")
    record = json.loads(intent.read_text())
    if record.get("run") != run["id"] or record.get("destination") != str(destination):
        raise RunnerRefused("restore receipt belongs to another run or destination")
    if not destination.exists():
        observed = "incomplete"
    else:
        observed = "complete" if destination.is_dir() and _digest(inventory(destination)) == record.get("sha256") else "changed"
    return {"state": observed, "run": run["id"], "destination": str(destination), "receipt": str(intent)}


def restore(run: dict, destination: Path) -> Path:
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Different source archive locks can still target one restore destination.
    identity = destination.parent.resolve() / destination.name
    token = hashlib.sha256(str(identity).encode()).hexdigest()[:24]
    with lock(destination.parent / f".sd-restore-{token}.lock", blocking=False):
        return _restore(run, destination)


def _restore(run: dict, destination: Path) -> Path:
    retained = Path(run["retained_path"])
    archive_path = retained.parent / "kept.tar"
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(str(destination).encode()).hexdigest()[:24]
    intent = destination.parent / f".sd-restore-{token}.json"
    staging = destination.parent / f".sd-restore-{token}.working"
    if intent.is_symlink() or staging.is_symlink() or destination.is_symlink():
        raise RunnerRefused("restore never overwrites a linked destination or intent")
    previous = json.loads(intent.read_text()) if intent.exists() else None
    identity = {"run": run["id"], "destination": str(destination)}
    if previous and any(previous.get(key) != value for key, value in identity.items()):
        raise RunnerRefused("restore destination belongs to a different recorded run")
    if destination.exists():
        if previous and destination.is_dir() and _digest(inventory(destination)) == previous.get("sha256"):
            return destination
        raise RunnerRefused(f"restore never overwrites an existing path: {destination}")
    archive = tarfile.open(archive_path) if not retained.is_dir() and archive_path.is_file() else None  # noqa: SIM115 - closed in finally across both source variants
    try:
        if retained.is_dir():
            manifest = inventory(retained)
            members = None
        elif archive:
            members = _archive_members(archive)
            manifest = {}
            for name, member in members.items():
                entry = {"mode": member.mode}
                if member.isdir():
                    entry["directory"] = True
                elif member.issym():
                    entry = {"link": member.linkname}
                else:
                    with archive.extractfile(member) as stream:
                        entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
                manifest[name] = entry
        else:
            raise RunnerRefused("no retained clone or complete kept archive exists")
        expected = {**identity, "sha256": _digest(manifest)}
        if previous and previous != expected:
            raise RunnerRefused("restore source changed since the interrupted copy")
        if staging.exists() and previous is None:
            raise RunnerRefused("unowned restore staging directory exists")
        _write(intent, expected)
        staging.mkdir(exist_ok=True)
        if set(inventory(staging)) - set(manifest):
            raise RunnerRefused("restore staging contains unrelated files")
        for directory, names, _files in os.walk(staging):
            Path(directory).chmod(0o700)
            for name in names:
                candidate = Path(directory) / name
                if not candidate.is_symlink():
                    candidate.chmod(0o700)
        for name, entry in sorted(manifest.items(), key=lambda pair: (len(Path(pair[0]).parts), pair[0])):
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if "link" in entry:
                if target.is_symlink() and os.readlink(target) == entry["link"]:
                    continue
                if target.exists() or target.is_symlink():
                    raise RunnerRefused("restore staging link conflicts with the source")
                target.symlink_to(entry["link"])
            elif entry.get("directory"):
                if target.is_symlink():
                    raise RunnerRefused("restore staging directory is a link")
                target.mkdir(exist_ok=True)
            else:
                if target.is_symlink() or target.is_dir():
                    raise RunnerRefused("restore staging file has changed type")
                if target.exists():
                    target.chmod(0o600)
                reader = (retained / name).open("rb") if members is None else archive.extractfile(members[name])
                with reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                target.chmod(entry["mode"])
        # Directory modes are applied after descendants are written.
        for name, entry in sorted(manifest.items(), reverse=True):
            if entry.get("directory"):
                (staging / name).chmod(entry["mode"])
        if inventory(staging) != manifest:
            raise RunnerRefused("restore listback differs from source bytes or modes")
        if destination.exists() or destination.is_symlink():
            raise RunnerRefused("restore destination appeared during copy")
        os.rename(staging, destination)
        fsync_directory(destination.parent)
        return destination
    finally:
        if archive:
            archive.close()
