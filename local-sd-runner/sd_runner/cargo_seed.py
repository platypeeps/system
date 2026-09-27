"""A new Rust clone starts from a copy of its repository's last passing build (sd:1814).

Every clone is new, so its `target/` is empty: assignment 114's check spent
all 900 seconds of `sd-check` compiling a large workspace's dependencies
from scratch. Sharing one target, or one Cargo `build-dir`, between clones is
wrong, not only slow: Cargo keys a workspace crate by name and version, not by
path, and judges it fresh by file times. A clone whose sources are older than
another clone's build then runs that other clone's binary.

So nothing is shared while Cargo runs. Each clone gets its own `target/`, a
copy-on-write copy (`cp -c`, times kept) of the repository's seed, and then
every tracked file in the clone is touched to now. The copied fingerprints are
older than every source in the clone, so the clone's own crates always
rebuild; a dependency whose sources live outside the clone, as registry
crates do under `~/.cargo/registry`, stays fresh. A Cargo that built the seed
with another compiler rebuilds everything: cold, never wrong.

A check that passed replaces the seed with a copy of that clone's `target/`,
copied aside and then renamed into place. A failed run never seeds. The copy
keeps only what builds dependencies: each profile's `.fingerprint/`, `build/`
and `deps/` without executables. A final output never enters the seed, so a
binary that a later branch removes cannot outlive it in a clone's `target/`. Every
failure here leaves the clone cold rather than stopping the run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path

#: The work root's folder of per-repository seeds; never an item id.
SEEDS = ".cargo-seed"
#: `/bin/cp`, not the search path's: GNU `cp` has no `-c`.
CP = "/bin/cp"
COPY_SECONDS = 600


def seed_path(work: Path, repo: str) -> Path:
    """The seed for a repository: its last part and a digest of its stored key."""
    digest = hashlib.sha256(repo.encode()).hexdigest()[:12]
    return work / SEEDS / f"{Path(repo).name}-{digest}"


def rust(clone: Path) -> bool:
    return (clone / "Cargo.toml").is_file()


def _copy(source: Path, destination: Path) -> bool:
    try:
        done = subprocess.run([CP, "-c", "-R", "-p", str(source), str(destination)],
                              capture_output=True, timeout=COPY_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _touch_tracked(clone: Path) -> None:
    listed = subprocess.run(["git", "-C", str(clone), "ls-files", "-z"], capture_output=True,
                            timeout=120, check=True)
    for name in listed.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not name:
            continue
        try:
            os.utime(clone / name, None, follow_symlinks=False)
        except FileNotFoundError:
            continue


#: What a profile folder (one holding `.fingerprint/`) keeps in the seed.
KEEP = {".fingerprint", "build", "deps"}


def _prune(folder: Path) -> None:
    """Remove every final output under a copied `target/`; keep dependency artifacts.

    A `deps/` entry without a dot is an executable, a bin or test target, and
    an entry that is a folder is its debug symbols (`.dSYM`); both go.
    """
    profile = (folder / ".fingerprint").is_dir()
    for entry in folder.iterdir():
        if profile and entry.name == "deps":
            for item in entry.iterdir():
                if "." not in item.name or (item.is_dir() and not item.is_symlink()):
                    _remove(item)
        elif profile and entry.name in KEEP:
            continue
        elif not profile and entry.is_dir() and not entry.is_symlink():
            _prune(entry)
        else:
            _remove(entry)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def seed(clone: Path, source: Path) -> bool:
    """Give a Rust clone without a `target/` a copy of the seed; True when it has one.

    The copy is pruned as `refresh` prunes a seed, so a seed written before
    pruning existed gives no final output either. A copy that fails, or one
    that cannot be pruned or whose clone's tracked files cannot all be
    touched, has its `target/` removed: the one unsafe state is a copy
    without both.
    """
    target = clone / "target"
    if not rust(clone) or os.path.lexists(target) or not source.is_dir():
        return False
    if not _copy(source, target):
        _discard(target)
        return False
    try:
        # A seed an older runner wrote whole still holds final outputs.
        _prune(target)
        _touch_tracked(clone)
    except (OSError, subprocess.SubprocessError):
        _discard(target)
        return False
    return True


def refresh(clone: Path, destination: Path) -> bool:
    """Replace the seed with a copy of a passing clone's `target/`; True when replaced.

    The copy is made beside the seed and renamed into place. A reader between
    the two renames finds no seed and builds cold; two refreshes at once each
    rename their own copy, and the later one wins.
    """
    target = clone / "target"
    if not rust(clone) or target.is_symlink() or not target.is_dir():
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}"
    if not _copy(target, staging):
        _discard(staging)
        return False
    try:
        _prune(staging)
    except OSError:
        _discard(staging)
        return False
    old = destination.parent / f".{destination.name}.old.{uuid.uuid4().hex}"
    try:
        if os.path.lexists(destination):
            os.rename(destination, old)
        os.rename(staging, destination)
    except OSError:
        _discard(staging)
        return False
    finally:
        _discard(old)
    return True
