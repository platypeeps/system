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
the label (sd:3042). The file length never changes, so mask writes each match
in place with one `pwrite` of its own span, on the same inode, and never
truncates. A session that appends while mask runs, through any descriptor,
keeps every byte: its writes land past the end mask read, which mask never
touches. Each span is read again just before its write. A span that changed,
or a failed or short write, stops the file, and mask prints the offset and
length of every match not yet fully written, because a half-masked value no
longer matches and the next run cannot find it. A failed fsync lists them
all. docs/work/2026-10-08-mask-rewrite/design.md has the failure table.
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


def masked(data: bytes, pairs, pats) -> tuple[bytes, int, int, list[tuple[int, int]]]:
    """Return the masked bytes, as long as `data`, the your-key count, the
    pattern count, and the sorted spans that changed, overlaps merged."""
    new, count, spans = data, 0, []
    for name, val in pairs:
        if not val:
            continue
        label = fit(b"<masked:$" + name.encode() + b">", len(val))
        found = [m.span() for m in re.finditer(re.escape(val), new)]
        if found:
            new = new.replace(val, label)
            count += len(found)
            spans += found
    pcount = 0
    for pat in pats:
        found = [m.span() for m in pat.finditer(new)]
        new = pat.sub(lambda m: fit(MARK, len(m.group(0))), new)
        pcount += len(found)
        spans += found
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        elif end > start:
            merged.append((start, end))
    return new, count, pcount, merged


def mask_file(path: str, pairs, pats, apply: bool) -> Result:
    """Mask one file in place, one pwrite per match span, without changing its length."""
    try:
        fd = os.open(path, os.O_RDWR if apply else os.O_RDONLY)
        writable = True
    except OSError as e:
        if not apply or e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        fd, writable = os.open(path, os.O_RDONLY), False
    with os.fdopen(fd, "rb") as f:
        data = f.read()
        new, count, pcount, spans = masked(data, pairs, pats)
        if not (count or pcount) or not apply:
            return Result(count, pcount)
        if not writable:
            raise WriteFailed("%s: not writable; %d match(es) found, none masked" % (path, count + pcount))

        def stop(reason: str, left: list[tuple[int, int]]) -> WriteFailed:
            lines = ["    offset %d length %d" % (start, end - start) for start, end in left]
            return WriteFailed("%s: %s; %d match span(s) not fully masked (a second run cannot find a"
                               " half-masked value; rotate its key):\n%s" % (path, reason, len(left), "\n".join(lines)))

        for i, (start, end) in enumerate(spans):
            try:
                if os.pread(fd, end - start, start) != data[start:end]:
                    raise stop("changed while it was masked", spans[i:])
                n = os.pwrite(fd, new[start:end], start)
            except OSError as e:
                raise stop(str(e), spans[i:]) from e
            if n != end - start:
                raise stop("short write, %d of %d bytes" % (n, end - start), spans[i:])
        try:
            os.fsync(fd)
        except OSError as e:
            raise stop("fsync: %s, nothing is known to be on disk" % e, spans) from e
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
