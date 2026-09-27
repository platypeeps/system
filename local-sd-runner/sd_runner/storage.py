"""Storage preflight and whole-clone retention, with no queue-path deletion."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sd_db.runner import RunnerRefused
from sd_db.runner_journal import fsync_directory, lock
from sd_db.runner_retention import PRUNING

CACHE_DIRECTORIES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", ".venv", "venv", ".sd-run"}
# Written into the clone by the runner itself, not by the run (sd:1771).
RUNNER_LOCAL_FILES = {"CLAUDE.local.md"}


class NoSpace(RunnerRefused):
    """A second copy -- the ignored copy or the kept archive -- met `ENOSPC`.

    The end run holds the row `ending` naming `no space` and retries the copy
    on every tick; the retained clone or the kept worktree already holds the
    bytes, so nothing is removed to make the copy fit. `path` is the copy
    that stopped, `needed` the bytes it still has to write.
    """

    def __init__(self, copy: str, path: Path, needed: int):
        super().__init__(f"no space: {copy} needs {needed} bytes at {path}")
        self.copy, self.path, self.needed = copy, path, needed


def _no_space(error: OSError) -> bool:
    return isinstance(error, OSError) and error.errno == errno.ENOSPC


def _remaining_ignored(retained: Path, target: Path, manifest: dict) -> int:
    """Bytes the ignored copy still has to write: every file not yet in place."""
    remaining = 0
    for name, expected in manifest.items():
        if "link" in expected:
            continue
        source, destination = retained / name, target / name
        if destination.is_file() and not destination.is_symlink() and destination.stat().st_size == source.stat().st_size:
            continue
        remaining += source.stat().st_size
    return remaining


def _cargo_output(root: Path, relative: Path, crates: dict) -> bool:
    """True when a `target` on the path sits beside a `Cargo.toml`.

    That is Cargo's build output: regenerable, and 30 GB in one clone, where
    digesting and copying it file by file held an ending for minutes (sd:1774).
    A `target` with no `Cargo.toml` beside it is not known to be Cargo's, so it
    is kept as run output. `Cargo.toml` is tracked, so a retained clone
    answers the same as the live one it was renamed from.
    """
    for index, part in enumerate(relative.parts):
        if part != "target":
            continue
        parent = relative.parts[:index]
        if parent not in crates:
            crates[parent] = (root.joinpath(*parent) / "Cargo.toml").is_file()
        if crates[parent]:
            return True
    return False


def _regenerable(root: Path, relative: Path, crates: dict) -> bool:
    """A known cache or Cargo build output; neither second copy keeps these."""
    return any(part in CACHE_DIRECTORIES for part in relative.parts) or _cargo_output(root, relative, crates)


def _left_out(root: Path, relative: Path, crates: dict) -> bool:
    """An ignored file the separate ignored copy does not keep.

    One rule for the inventory and for its replay: a manifest stored before
    sd:1774 still lists Cargo output, and replaying it copied 30 GB.
    """
    return (_regenerable(root, relative, crates) or relative.name.endswith(".pyc") or relative.name == ".DS_Store"
            or str(relative) in RUNNER_LOCAL_FILES)


def archive_skip(clone: Path):
    """The whole-clone archive's exclusion: ignored caches and Cargo output.

    A path is left out only when Git reports it untracked and ignored and its
    name marks it a cache or Cargo output. A directory name alone proves
    nothing: an unstaged edit to a tracked `venv/source.py` or a vendored
    `node_modules` lives only in the working tree, so it is archived. Git
    that cannot answer leaves nothing out. `walk`, `restoration.inventory`
    and the tar all take this one predicate, so the listback and a refresh's
    comparison agree with the tar.
    """
    listing = subprocess.run(["git", "-C", str(clone), "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"],
                             capture_output=True, check=False)
    ignored = set()
    if listing.returncode == 0:
        # `--directory` names a wholly ignored directory once, with a trailing slash.
        ignored = {Path(name) for name in listing.stdout.decode().split("\0") if name}
    crates = {}

    def skip(relative: Path) -> bool:
        if relative.parts[:1] == (".git",) or not (relative in ignored or any(parent in ignored for parent in relative.parents)):
            return False
        return _regenerable(clone, relative, crates)
    return skip


def kept_ignored(retained: Path, manifest: dict) -> dict:
    """The entries of a stored manifest the ignored copy still keeps.

    An unsafe name is refused here, before the space estimate reads it.
    """
    crates, kept = {}, {}
    for name, expected in manifest.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RunnerRefused("unsafe preserved-output path")
        if not _left_out(retained, relative, crates):
            kept[name] = expected
    return kept


def _hold_floor(copy: str, path: Path, needed: int, floor_gb) -> None:
    """Refuse a copy that would leave less than the free-space floor (sd:1774).

    The same hold as a copy that met `ENOSPC`: the row waits naming `no
    space`, and the next tick measures again. Nothing to write never holds.
    """
    if floor_gb is None or needed <= 0:
        return
    if capacity(path)["free"] - needed < floor_gb * 1e9:
        raise NoSpace(copy, path, needed)


def ignored_manifest(clone: Path, *, progress=None) -> dict:
    """Inventory before freezing; no Git command runs against retained clones.

    `progress` is called once per file digested, so a caller can show it is
    alive through a long inventory.
    """
    from .gitops import git
    result = subprocess.run(["git", "-C", str(clone), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                            capture_output=True, check=True)
    forward = Path(git(clone, "config", "--get", "sd.hooksForward", check=False) or clone / ".git/hooks")
    records, crates = {}, {}
    for name in result.stdout.decode().split("\0"):
        if not name:
            continue
        relative = Path(name)
        source = clone / relative
        if relative.is_absolute() or ".." in relative.parts:
            raise RunnerRefused("ignored inventory contains an unsafe relative path")
        if _left_out(clone, relative, crates):
            continue
        if source == forward or forward in source.parents:
            continue
        if source.is_symlink():
            records[name] = {"link": os.readlink(source)}
        elif source.is_file():
            if progress is not None:
                progress()
            with source.open("rb") as reader:
                records[name] = {"sha256": hashlib.file_digest(reader, "sha256").hexdigest(), "mode": stat.S_IMODE(source.stat().st_mode)}
        else:
            raise RunnerRefused(f"ignored inventory contains a special file: {name}")
    return records


def preserve_ignored(retained: Path, manifest: dict, *, progress=None, floor_gb=None) -> None:
    """Replay a frozen run's copies; preserve these beyond clone retention.

    The replay applies the inventory's rule again, so a manifest stored
    before that rule copies no Cargo output. `progress` is called before each
    entry, as in `ignored_manifest`. With `floor_gb`, a copy that would cross
    that floor is refused as `NoSpace` before it writes.
    """
    target = retained.parent / "ignored"
    manifest = kept_ignored(retained, manifest)
    _hold_floor("ignored copy", target, _remaining_ignored(retained, target, manifest), floor_gb)
    try:
        for name, expected in manifest.items():
            if progress is not None:
                progress()
            _preserve_one(retained, target, name, expected)
    except OSError as error:
        if not _no_space(error):
            raise
        # The partial file stays where it stopped; the next tick rewrites it.
        raise NoSpace("ignored copy", target, _remaining_ignored(retained, target, manifest)) from error


def _preserve_one(retained: Path, target: Path, name: str, expected: dict) -> None:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise RunnerRefused("unsafe preserved-output path")
    source, destination = retained / relative, target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in destination.parents if parent != target.parent):
        raise RunnerRefused("preserved output path traverses a symlink")
    if "link" in expected:
        if not source.is_symlink() or os.readlink(source) != expected["link"]:
            raise RunnerRefused("retained ignored link differs from its inventory")
        if destination.is_symlink() and os.readlink(destination) == expected["link"]:
            return
        if destination.exists() or destination.is_symlink():
            raise RunnerRefused("preserved ignored link conflicts with existing output")
        destination.symlink_to(expected["link"])
    else:
        if source.is_symlink() or destination.is_symlink():
            raise RunnerRefused("preserved ignored file changed type")
        if destination.is_file():
            # A retry rewrote a file the previous pass had already renamed
            # into place, needing space `_remaining_ignored` had counted as
            # written (sd:1221). The digest is what says it is in place; a
            # destination that differs is still rewritten below.
            with destination.open("rb") as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() == expected["sha256"]:
                    if stat.S_IMODE(destination.stat().st_mode) != expected["mode"]:
                        destination.chmod(expected["mode"])
                    return
        temporary = destination.with_name(destination.name + ".sd-copy-partial")
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        with temporary.open("rb") as reader:
            if hashlib.file_digest(reader, "sha256").hexdigest() != expected["sha256"]:
                raise RunnerRefused("retained ignored bytes differ from their inventory")
        temporary.chmod(expected["mode"])
        os.replace(temporary, destination)
    fsync_directory(destination.parent)


def capacity(path: Path) -> dict:
    current = path
    while not current.exists():
        if current.parent == current:
            raise RunnerRefused(f"cannot find storage ancestor: {path}")
        current = current.parent
    value = os.statvfs(current)
    return {"device": current.stat().st_dev, "free": value.f_bavail * value.f_frsize,
            "total": value.f_blocks * value.f_frsize}


DISKUTIL_TIMEOUT = 20


def _diskutil(*argv: str) -> subprocess.CompletedProcess | None:
    """One `diskutil` call, or None when it timed out.

    A slow `diskutil` (the volume asleep, Spotlight on it) is a problem the
    caller records beside a non-zero exit, not a `TimeoutExpired` raised out
    of `serve` and into launchd's restart loop (sd:970).
    """
    try:
        return subprocess.run(["diskutil", *argv], capture_output=True, timeout=DISKUTIL_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return None


def preflight(database: Path, work: Path, retained: Path, *, floor_gb=40) -> dict:
    if floor_gb <= 0:
        raise RunnerRefused("free-space floor must be positive")
    db, active, backup = capacity(database), capacity(work), capacity(retained)
    problems = []
    quota = None
    if not work.is_dir() or not retained.is_dir():
        problems.append("worktrees and retained-clone directories must exist on the provisioned work volume")
    if active["device"] == db["device"]:
        problems.append("database and worktrees share a device; provision the separate quota-limited APFS sd-work volume")
    if active["device"] != backup["device"]:
        problems.append("worktrees and retained clones must share one device for atomic rename")
    if active["total"] <= floor_gb * 1e9:
        problems.append(f"work-volume quota must exceed free_floor_gb={floor_gb}")
    if sys.platform == "darwin" and work.exists():
        # diskutil accepts a mountpoint, not a nested directory or its alias.
        mount = work.resolve()
        while not mount.is_mount() and mount.parent != mount:
            mount = mount.parent
        timed_out = f"diskutil timed out after {DISKUTIL_TIMEOUT}s"
        done = _diskutil("info", "-plist", str(mount))
        if done is None:
            problems.append(f"cannot verify work volume is APFS ({timed_out})")
        elif done.returncode:
            problems.append("cannot verify work volume is APFS")
        else:
            import plistlib
            info = plistlib.loads(done.stdout)
            if str(info.get("FilesystemType", "")).lower() != "apfs":
                problems.append("work volume must be APFS for immutable clone retention")
            else:
                inventory = _diskutil("apfs", "list", "-plist")
                if inventory is None:
                    problems.append(f"cannot verify configured APFS capacity quota ({timed_out})")
                elif inventory.returncode:
                    problems.append("cannot verify configured APFS capacity quota")
                else:
                    volumes = [volume for container in plistlib.loads(inventory.stdout).get("Containers", [])
                               for volume in container.get("Volumes", []) if volume.get("DeviceIdentifier") == info.get("DeviceIdentifier")]
                    quota = volumes[0].get("CapacityQuota") if len(volumes) == 1 else None
                    if type(quota) is not int or quota <= floor_gb * 1e9:
                        problems.append(f"APFS CapacityQuota must be configured and exceed {floor_gb} GB; observed {quota}")
    elif sys.platform != "darwin":
        problems.append("production runner storage requires macOS APFS")
    return {"ok": not problems, "problems": problems, "database": db, "work": active, "retention": backup,
            "dispatch_allowed": not problems and active["free"] >= floor_gb * 1e9 and db["free"] >= floor_gb * 1e9,
            "database_below_floor": db["free"] < floor_gb * 1e9, "free_floor_gb": floor_gb, "quota_bytes": quota}


def walk(path: Path, *, skip=None) -> dict:
    """Every entry under `path`; a `skip` predicate on a relative path prunes it and what it holds."""
    result = {}
    for root, directories, files in os.walk(path, followlinks=False):
        if skip is not None:
            base = Path(root).relative_to(path)
            directories[:] = [name for name in directories if not skip(base / name)]
            files = [name for name in files if not skip(base / name)]
        for name in sorted(directories + files):
            candidate = Path(root) / name
            value = candidate.lstat()
            result[str(candidate.relative_to(path))] = (value.st_mode, value.st_size, value.st_mtime_ns,
                                                       value.st_ino, os.readlink(candidate) if candidate.is_symlink() else None)
    return result


def tar_needed(entries: dict) -> int:
    """Bytes an archive of a `walk` inventory needs, payload and overhead.

    `TarFile.add` writes a header, padding and a PAX path for every entry,
    so the payload alone underestimates what the next attempt has to fit
    (sd:1221). This is the estimate `archive_refresh` checks free space
    against before it writes a generation; one formula, both callers.
    """
    return sum(value[1] + 4096 + len(name.encode()) * 2 + len((value[4] or "").encode()) * 2
               for name, value in entries.items()) + 10240


def archive(clone: Path, destination: Path, *, progress=None, floor_gb=None) -> Path:
    """Archive the whole clone but its caches and Cargo output (`archive_skip`).

    `progress` is called per entry written and per file read back. With
    `floor_gb`, an archive that would cross that floor is refused as
    `NoSpace` before any byte is written (sd:1774).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    skip = archive_skip(clone)

    def admit(member):
        if progress is not None:
            progress()
        relative = Path(*Path(member.name).parts[1:])
        return None if relative.parts and skip(relative) else member

    with lock(destination.parent / ".archive.lock"):
        before = walk(clone, skip=skip)
        partial = destination.with_suffix(".partial")
        _hold_floor("kept archive", partial, tar_needed(before), floor_gb)
        try:
            with tarfile.open(partial, "w", dereference=False) as output:
                output.add(clone, arcname="clone", recursive=True, filter=admit)
            if walk(clone, skip=skip) != before:
                raise RunnerRefused(f"clone changed during archive; retained partial at {partial}")
            with tarfile.open(partial) as check:
                names = set(check.getnames())
                if {"clone", *("clone/" + name for name in before)} != names:
                    raise RunnerRefused("archive listback differs from source inventory")
                for member in check.getmembers():
                    if progress is not None:
                        progress()
                    if member.isfile():
                        handle = check.extractfile(member)
                        if handle is None:
                            raise RunnerRefused("archive file is unreadable")
                        while handle.read(1024 * 1024):
                            pass
            # Delayed allocation reports a full volume at the flush, not at
            # the write, so the fsync and the rename are inside the
            # translation too; outside it they fell through as a generic
            # OSError and the hold never said "no space" (sd:1221).
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial, destination)
            fsync_directory(destination.parent)
        except OSError as error:
            if not _no_space(error):
                raise
            # The partial archive stays; the next tick writes it again.
            raise NoSpace("kept archive", partial, tar_needed(before)) from error
        return destination


def freeze(path: Path) -> None:
    if sys.platform != "darwin":
        raise RunnerRefused("immutable production retention requires macOS APFS")
    subprocess.run(["chflags", "-R", "uchg", str(path)], check=True, capture_output=True)
    immutable = stat.UF_IMMUTABLE
    for root, directories, files in os.walk(path):
        for candidate in [Path(root), *(Path(root) / name for name in directories + files)]:
            if not candidate.is_symlink() and not candidate.stat().st_flags & immutable:
                raise RunnerRefused(f"immutable flag missing after retention: {candidate}")


def retain(clone: Path, destination: Path, *, freezer=freeze) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if clone.exists() and destination.exists():
        raise RunnerRefused(f"both active and retained clones exist; refusing overwrite: {destination}")
    if clone.exists():
        if clone.stat().st_dev != destination.parent.stat().st_dev:
            raise RunnerRefused("retention would cross devices")
        os.rename(clone, destination)
        fsync_directory(clone.parent)
        fsync_directory(destination.parent)
    if not destination.is_dir():
        raise RunnerRefused("neither active nor retained clone exists")
    receipt = destination.parent / "retention.json"
    if not receipt.exists():
        with receipt.open("x") as handle:
            json.dump({"retained_at": datetime.now(UTC).isoformat(), "clone": str(destination)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(destination.parent)
    freezer(destination)


def restore(run: dict, destination: Path) -> Path:
    from .restoration import restore as replay
    retained = Path(run["retained_path"])
    with lock(retained.parent / ".archive.lock"):
        leftover = retained.parent / PRUNING
        if os.path.lexists(leftover):
            # A prune that stopped part way; what is left is not a clone, and
            # the kept archive beside it is not read while it stands (sd:770).
            raise RunnerRefused(f"a retained clone removal stopped part way at {leftover}; "
                                "finish it with `runner.sh prune` and `prune-apply` before restoring this run")
        if not retained.is_dir():
            from .archive_refresh import latest
            token = hashlib.sha256(str(destination.absolute()).encode()).hexdigest()[:24]
            intent = destination.parent / f".sd-restore-{token}.json"
            expected = json.loads(intent.read_text()).get("sha256") if intent.is_file() and not intent.is_symlink() else None
            run = latest(run, expected_sha256=expected)
        return replay(run, destination)


def prune_inventory(retention: Path, *, days=30) -> list[dict]:
    if days < 30:
        raise RunnerRefused("retention cannot be shorter than 30 days")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = []
    for clone in sorted(retention.glob("*/*/clone")):
        receipt = clone.parent / "retention.json"
        if not receipt.is_file():
            continue  # Unproven age never authorizes removing a retained clone.
        when = datetime.fromisoformat(json.loads(receipt.read_text())["retained_at"])
        if when <= cutoff:
            result.append({"path": str(clone), "retained_before": when.isoformat(), "action": "requires explicit deletion approval"})
    return result
