#!/usr/bin/env python3
"""The rewrite half of `scan-for-secrets.sh mask`: one file path per stdin line.

Your key values arrive in S4S_PAIRS (NAME=value lines) and the credential
patterns in S4S_PATTERNS, both through the environment, never argv or disk.
S4S_APPLY=1 rewrites; anything else is a dry run that exits 2 when there is
something to mask.

Besides your literal values, the well-known credential patterns are masked
too, with two adaptations: the URL-credentials class must not swallow quotes
or backslashes (it would corrupt JSONL), and PEM masking covers the whole
BEGIN..END block, not just the header line.

Every mask is as long as the bytes it replaces: `<masked:$NAME>` or
`<masked:pattern>`, padded with `*`, or all `*` when the match is shorter than
the label (sd:3042). The file length never changes, so mask writes only the
changed blocks in place, on the same inode, and never truncates. A session
that appends while mask runs, through any descriptor, keeps every byte: its
writes land past the end mask read, which mask never touches. Before each
block is written it is read again; a block that changed since the first read
stops the file as FAILED. docs/work/2026-10-08-mask-rewrite/design.md has the
failure table.
"""

from __future__ import annotations

import errno
import os
import re
import sys
from typing import NamedTuple

PEM = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    rb"(?:[\s\S]{0,10000}?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----)?"
)
MARK = b"<masked:pattern>"
BLOCK = 4096


class WriteFailed(Exception):
    """A file that holds a match and was not fully masked.
    Not an OSError, so the read-time skip in `main` does not swallow it."""


class Result(NamedTuple):
    count: int
    pcount: int


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


def fit(label: bytes, n: int) -> bytes:
    """`label` padded with `*` to `n` bytes, or `n` stars when it does not fit."""
    return label + b"*" * (n - len(label)) if n >= len(label) else b"*" * n


def masked(data: bytes, pairs, pats) -> tuple[bytes, int, int]:
    """Return the masked bytes, as long as `data`, the your-key count and the pattern count."""
    new, count = data, 0
    for name, val in pairs:
        c = new.count(val)
        if c:
            new = new.replace(val, fit(b"<masked:$" + name.encode() + b">", len(val)))
            count += c
    pcount = 0
    for pat in pats:
        new, n = pat.subn(lambda m: fit(MARK, len(m.group(0))), new)
        pcount += n
    return new, count, pcount


def pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        view, offset = view[n:], offset + n


def mask_file(path: str, pairs, pats, apply: bool) -> Result:
    """Mask one file in place, block by block, without changing its length."""
    try:
        fd = os.open(path, os.O_RDWR if apply else os.O_RDONLY)
        writable = True
    except OSError as e:
        if not apply or e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        fd, writable = os.open(path, os.O_RDONLY), False
    with os.fdopen(fd, "rb") as f:
        data = f.read()
        new, count, pcount = masked(data, pairs, pats)
        if not (count or pcount) or not apply:
            return Result(count, pcount)
        left = "%d match(es) found" % (count + pcount)
        if not writable:
            raise WriteFailed("%s: not writable; %s, none masked" % (path, left))
        written = 0
        try:
            for start in range(0, len(data), BLOCK):
                old = data[start:start + BLOCK]
                block = new[start:start + BLOCK]
                if old == block:
                    continue
                if os.pread(fd, len(old), start) != old:
                    raise WriteFailed("%s: changed while it was masked; %s, %d block(s) masked before it" % (
                        path, left, written))
                pwrite_all(fd, block, start)
                written += 1
            os.fsync(fd)
        except OSError as e:
            raise WriteFailed("%s: %s; %s, %d block(s) masked before the error; run mask again" % (
                path, e, left, written)) from e
    return Result(count, pcount)


def main() -> int:
    apply = os.environ.get("S4S_APPLY") == "1"
    pairs = parse_pairs(os.environ.get("S4S_PAIRS", ""))
    pats = parse_patterns(os.environ.get("S4S_PATTERNS", ""))
    total = ptotal = files = failed = 0
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
        count, pcount = result.count, result.pcount
        if count or pcount:
            files += 1
            total += count
            ptotal += pcount
            print("  %s: %d your-key value(s), %d pattern match(es)" % (path, count, pcount))
    if apply:
        print("== masked %d your-key value(s) + %d pattern match(es) in %d file(s); %d failed"
              % (total, ptotal, files, failed))
        return 1 if failed else 0
    print("== would mask %d your-key value(s) + %d pattern match(es) in %d file(s)"
          " (dry run; add --apply)" % (total, ptotal, files))
    return 2 if (total or ptotal) else 0


if __name__ == "__main__":
    sys.exit(main())
