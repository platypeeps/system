"""Move an idle cargo target/ into the prune trash under cargo's lock (sd:5994).

maintenance.sh runs `python3 prune_move.py SRC DST`. Cargo locks a build
with flock(2) on target/<profile>/.cargo-lock, so this takes every such lock
without waiting and renames SRC to DST while it holds them all.

A build can make and lock a new profile's lock after the first pass and
before the rename. So after the rename this locks every lock in DST too.
A busy or unreadable one sends DST back to SRC. After the rename no path
leads into DST, so no build can lock there later.

Exit 0: moved. 75: a build holds a lock; nothing moved. 76: a build started
during the move; DST went back to SRC. 77: as 76, but SRC exists again, so
DST stays where it is, named on stderr. 1: an error, named on stderr;
nothing moved.

`python3 prune_move.py DIR` only checks: exit 75 when a build holds a lock
under DIR, 1 on an error, else 0.
"""

from __future__ import annotations

import fcntl
import glob
import os
import sys

#: Where cargo puts its locks: the target, a profile, a target triple's profile.
PATTERNS = (".cargo-lock", "*/.cargo-lock", "*/*/.cargo-lock")
BUSY, RESTORED, KEPT = 75, 76, 77


class Busy(Exception):
    """A build holds the named lock."""


def lock_all(root: str, held: dict[tuple[int, int], int]) -> None:
    """flock every .cargo-lock under `root` that `held` does not hold yet.

    `held` maps each lock's (device, inode) to its descriptor, and gains each
    one opened here, so `release` closes it whatever happens. Raises `Busy`.
    """
    for pattern in PATTERNS:
        for path in glob.glob(os.path.join(glob.escape(root), pattern)):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            details = os.fstat(fd)
            key = (details.st_dev, details.st_ino)
            if key in held:
                os.close(fd)
                continue
            held[key] = fd
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy(path) from None


def release(held: dict[tuple[int, int], int]) -> None:
    for fd in held.values():
        os.close(fd)
    held.clear()


def prepass(src: str) -> dict[tuple[int, int], int]:
    """Take every lock under `src`; the caller releases them."""
    held: dict[tuple[int, int], int] = {}
    try:
        lock_all(src, held)
    except BaseException:
        release(held)
        raise
    return held


def move(src: str, dst: str, held: dict[tuple[int, int], int]) -> int:
    """Rename `src` to `dst` while the caller holds the prepass locks."""
    os.rename(src, dst)
    return settle(src, dst, held)


def settle(src: str, dst: str, held: dict[tuple[int, int], int]) -> int:
    """After the rename, lock what a build locked in the window, or undo it."""
    try:
        lock_all(dst, held)
        return 0
    except (Busy, OSError) as error:
        why = error
    # rename(2) back fails on a SRC that holds anything, and replaces only
    # an empty folder.
    try:
        os.rename(dst, src)
        return RESTORED
    except OSError as error:
        print(f"{why}; {error}", file=sys.stderr)
        return KEPT


def main(argv: list[str]) -> int:
    held: dict[tuple[int, int], int] = {}
    try:
        held = prepass(argv[1])
        if len(argv) == 2:
            return 0
        return move(argv[1], argv[2], held)
    except Busy:
        return BUSY
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        release(held)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
