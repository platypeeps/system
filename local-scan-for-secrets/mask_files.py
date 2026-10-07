#!/usr/bin/env python3
"""The rewrite half of `scan-for-secrets.sh mask`: one file path per stdin line.

Your key values arrive in S4S_PAIRS (NAME=value lines) and the credential
patterns in S4S_PATTERNS, both through the environment, never argv or disk.
S4S_APPLY=1 rewrites; anything else is a dry run that exits 2 when there is
something to mask.

The rewrite is in place on the same inode (open r+, write, truncate), so a
file a live session holds open keeps working. Besides your literal values,
the well-known credential patterns are masked too, with two adaptations: the
URL-credentials class must not swallow quotes or backslashes (it would
corrupt JSONL), and PEM masking covers the whole BEGIN..END block, not just
the header line.

A live session may append to a log while it is rewritten, and a write of the
masked bytes plus a truncate would drop that append (sd:1254). Two guards
skip such a file, which then counts as busy and waits for the next run:

1. Settle age: a file modified within S4S_MASK_SETTLE_MIN minutes (default
   10) is in use now.
2. Compare before write: the size and mtime are read again on the open handle
   just before the write; a change since the read means a writer got in.

The window left is between that fstat and the truncate, microseconds against
a session that appends seconds apart. A temp file and a rename would close
it, but a session holding the old inode would then write into an unlinked
file, and Claude Code and Codex take no lock a flock could wait on.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import NamedTuple

PEM = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    rb"(?:[\s\S]{0,10000}?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----)?"
)
MARK = b"<masked:pattern>"
SETTLE_MIN_DEFAULT = 10


class WriteFailed(Exception):
    """A rewrite that failed after it began; the file may be half masked.
    Not an OSError, so the read-time skip in `main` does not swallow it."""


class Result(NamedTuple):
    count: int
    pcount: int
    busy: bool = False


def parse_pairs(text: str) -> list[tuple[str, bytes]]:
    pairs = []
    for line in text.split("\n"):
        if "=" in line:
            name, val = line.split("=", 1)
            pairs.append((name, val.encode()))
    return pairs


def parse_patterns(text: str) -> list[re.Pattern]:
    pats = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pats.append(PEM if "PRIVATE KEY" in line else re.compile(line.encode()))
    return pats


def masked(data: bytes, pairs, pats) -> tuple[bytes, int, int]:
    """Return the masked bytes, the your-key count and the pattern count."""
    new, count = data, 0
    for name, val in pairs:
        c = new.count(val)
        if c:
            new = new.replace(val, b"<masked:$" + name.encode() + b">")
            count += c
    pcount = 0
    for pat in pats:
        new, n = pat.subn(MARK, new)
        pcount += n
    return new, count, pcount


def mask_file(path: str, pairs, pats, apply: bool, settle_s: float = 0.0, before_write=None) -> Result:
    """Mask one file, or leave it alone as busy.

    `before_write` is called with the path between the read and the write.
    It exists for the tests, which append to the file there.
    """
    if time.time() - os.stat(path).st_mtime < settle_s:
        return Result(0, 0, busy=True)
    # Any OSError once the rewrite starts, up to and including the close
    # that flushes what the buffer still holds, may leave the file half
    # masked; before it, the file is untouched and a skip is right. A file
    # that will not open to write fails only when it holds a match: the
    # value stays on disk, so the run must not read as a success.
    started = opened = False
    try:
        with open(path, "r+b" if apply else "rb") as f:
            opened = True
            seen = os.fstat(f.fileno())
            data = f.read()
            new, count, pcount = masked(data, pairs, pats)
            if not (count or pcount) or not apply:
                return Result(count, pcount)
            if before_write:
                before_write(path)
            now = os.fstat(f.fileno())
            if (now.st_size, now.st_mtime_ns) != (seen.st_size, seen.st_mtime_ns):
                return Result(0, 0, busy=True)
            started = True
            f.seek(0)
            f.write(new)
            f.truncate()
            f.flush()
    except OSError as e:
        if started:
            raise WriteFailed("%s: %s; %s" % (path, e, recover(path, data, new, (seen.st_dev, seen.st_ino)))) from e
        if apply and not opened:
            left = mask_file(path, pairs, pats, False)
            if left.count or left.pcount:
                raise WriteFailed("%s: %s; %d match(es) left unmasked"
                                  % (path, e, left.count + left.pcount)) from e
            return left
        raise
    return Result(count, pcount)


def cut_short(on_disk: bytes, data: bytes, new: bytes) -> bool:
    """True when `on_disk` is `new[:k] + data[k:]` for some k: what an
    in-place write leaves when it stops partway, before the truncate."""
    if len(on_disk) != len(data):
        return len(on_disk) > len(data) and new.startswith(on_disk)
    written = len(os.path.commonprefix([on_disk, new]))
    kept = len(os.path.commonprefix([on_disk[::-1], data[::-1]]))
    return len(data) - kept <= written


def recover(path: str, data: bytes, new: bytes, identity: tuple[int, int]) -> str:
    """After a failed rewrite, leave the file whole: as it was, or masked.

    It runs once the buffered handle is closed, since a close flushes what
    the buffer held over anything written before it. Only the file that was
    read is touched (same device and inode), and only when its bytes are
    exactly what a write cut short leaves: a session's append since, or a
    rotated log, stays as it is. A file that holds either version stays too,
    since undoing a rewrite that landed would put the value back. A cut-short
    write gets the original bytes back; the run still fails, and the next
    one masks it.
    """
    try:
        fd = os.open(path, os.O_RDWR)
        with os.fdopen(fd, "r+b", buffering=0) as raw:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != identity:
                return "file replaced after the failed write; left as is"
            on_disk = raw.read()
            if on_disk == new:
                return "the masked bytes landed"
            if on_disk == data:
                return "file unchanged"
            if not cut_short(on_disk, data, new) or os.fstat(fd).st_size != len(on_disk):
                return "file changed after the failed write; left as is, and it may be half masked"
            raw.seek(0)
            view = memoryview(data)
            while view:
                view = view[raw.write(view):]
            raw.truncate(len(data))
            os.fsync(fd)
    except OSError as e:
        return "restore failed (%s); the file may be half masked" % e
    return "original restored"


def settle_seconds(text: str) -> float:
    try:
        minutes = float(text)
    except ValueError:
        sys.exit("mask_files.py: S4S_MASK_SETTLE_MIN must be a number of minutes, not %r" % text)
    if minutes < 0:
        sys.exit("mask_files.py: S4S_MASK_SETTLE_MIN must not be negative, not %r" % text)
    return minutes * 60


def main() -> int:
    apply = os.environ.get("S4S_APPLY") == "1"
    pairs = parse_pairs(os.environ.get("S4S_PAIRS", ""))
    pats = parse_patterns(os.environ.get("S4S_PATTERNS", ""))
    settle_s = settle_seconds(os.environ.get("S4S_MASK_SETTLE_MIN", str(SETTLE_MIN_DEFAULT)))
    total = ptotal = files = busy = failed = 0
    for path in sys.stdin.read().splitlines():
        if not path:
            continue
        try:
            result = mask_file(path, pairs, pats, apply, settle_s)
        except OSError as e:
            print("  skip %s: %s" % (path, e), file=sys.stderr)
            continue
        except WriteFailed as e:
            print("  FAILED %s" % e, file=sys.stderr)
            failed += 1
            continue
        if result.busy:
            busy += 1
            print("  busy %s" % path)
            continue
        count, pcount = result.count, result.pcount
        if count or pcount:
            files += 1
            total += count
            ptotal += pcount
            print("  %s: %d your-key value(s), %d pattern match(es)" % (path, count, pcount))
    if apply:
        print("== masked %d your-key value(s) + %d pattern match(es) in %d file(s); %d busy; %d failed"
              % (total, ptotal, files, busy, failed))
        return 1 if failed else 0
    print("== would mask %d your-key value(s) + %d pattern match(es) in %d file(s); %d busy"
          " (dry run; add --apply)" % (total, ptotal, files, busy))
    return 2 if (total or ptotal) else 0


if __name__ == "__main__":
    sys.exit(main())
