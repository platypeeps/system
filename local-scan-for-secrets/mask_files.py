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
masked bytes plus a truncate would drop that append (sd:1254). So the size
and mtime are read again on the open handle just before the write; a change
since the read skips the file, which counts as busy and waits for the next
run. Run the mask by hand at a quiet time.

The window left is between that fstat and the truncate, microseconds against
a session that appends seconds apart. A temp file and a rename would close
it, but a session holding the old inode would then write into an unlinked
file, and Claude Code and Codex take no lock a flock could wait on.
"""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

PEM = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    rb"(?:[\s\S]{0,10000}?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----)?"
)
MARK = b"<masked:pattern>"


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


def mask_file(path: str, pairs, pats, apply: bool, before_write=None) -> Result:
    """Mask one file, or leave it alone as busy.

    `before_write` is called with the path between the read and the write.
    It exists for the tests, which append to the file there.
    """
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
            raise WriteFailed("%s: %s; the file may be half masked" % (path, e)) from e
        if apply and not opened:
            left = mask_file(path, pairs, pats, False)
            if left.count or left.pcount:
                raise WriteFailed("%s: %s; %d match(es) left unmasked"
                                  % (path, e, left.count + left.pcount)) from e
            return left
        raise
    return Result(count, pcount)


def main() -> int:
    apply = os.environ.get("S4S_APPLY") == "1"
    pairs = parse_pairs(os.environ.get("S4S_PAIRS", ""))
    pats = parse_patterns(os.environ.get("S4S_PATTERNS", ""))
    total = ptotal = files = busy = failed = 0
    for path in sys.stdin.read().splitlines():
        if not path:
            continue
        try:
            result = mask_file(path, pairs, pats, apply)
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
    print("== would mask %d your-key value(s) + %d pattern match(es) in %d file(s)"
          " (dry run; add --apply)" % (total, ptotal, files))
    return 2 if (total or ptotal) else 0


if __name__ == "__main__":
    sys.exit(main())
