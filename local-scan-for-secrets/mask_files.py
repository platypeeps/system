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

The masked bytes go to a temp file in the same folder, which is fsynced and
renamed over the original (sd:3042). Until the rename the original is
untouched, so a full disk or an I/O error leaves it whole. A file another
process holds open, such as a live session's log, is skipped as busy and
waits for the next run; masking is manual, so a later run is cheap. A
writer that got in after that check is caught twice: a size or mtime change
on the read handle before the rename skips the file as busy, and bytes that
reach the old inode during the rename are masked and appended to the new
file. docs/work/2026-10-08-mask-rewrite/design.md has the failure table.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

PEM = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    rb"(?:[\s\S]{0,10000}?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----)?"
)
MARK = b"<masked:pattern>"


class WriteFailed(Exception):
    """A file that holds a match and was not masked; the original is whole.
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


def holders(path: str) -> list[int] | None:
    """Other processes holding `path` open, or None when nothing can tell.

    Linux answers from /proc; macOS through lsof, whose exit 1 with no output
    means nobody holds it.
    """
    me = os.getpid()
    if os.path.isdir("/proc/self/fd"):
        found = set()
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                fds = os.listdir("/proc/%s/fd" % pid)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == path:
                        found.add(int(pid))
                except OSError:
                    continue
        return sorted(found - {me})
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.access("/usr/sbin/lsof", os.X_OK) else None)
    if lsof is None:
        return None
    done = subprocess.run([lsof, "-t", "--", path], capture_output=True, text=True, timeout=60)
    if done.returncode not in (0, 1):
        return None
    return sorted({int(pid) for pid in done.stdout.split()} - {me})


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def mask_file(path: str, pairs, pats, apply: bool, before_rename=None) -> Result:
    """Mask one file, or leave it alone as busy.

    `before_rename` is called with the path once the temp file is synced and
    before the rename. It exists for the tests, which append to the file there.
    """
    path = os.path.realpath(path)
    with open(path, "rb") as f:
        seen = os.fstat(f.fileno())
        data = f.read()
        new, count, pcount = masked(data, pairs, pats)
        if not (count or pcount) or not apply:
            return Result(count, pcount)
        left = "%d match(es) left unmasked; the original is unchanged" % (count + pcount)
        held = holders(path)
        if held is None:
            raise WriteFailed("%s: cannot tell whether another process holds it open (no lsof); %s" % (path, left))
        if held:
            return Result(0, 0, busy=True)
        if seen.st_nlink > 1:
            raise WriteFailed("%s: has %d hard links, and a rename would split them; %s" % (path, seen.st_nlink, left))
        if not os.access(path, os.W_OK):
            raise WriteFailed("%s: not writable; %s" % (path, left))
        folder, name = os.path.split(path)
        try:
            fd, temp = tempfile.mkstemp(prefix=".%s.mask-" % name, dir=folder)
        except OSError as e:
            raise WriteFailed("%s: %s; %s" % (path, e, left)) from e
        try:
            try:
                os.fchmod(fd, seen.st_mode & 0o7777)
                write_all(fd, new)
                os.fsync(fd)
            finally:
                os.close(fd)
            if before_rename:
                before_rename(path)
            # Asked again just before the rename: a session that opened the
            # file since the first check, or wrote to it, makes it busy.
            now, there = os.fstat(f.fileno()), os.stat(path)
            if (now.st_size, now.st_mtime_ns) != (seen.st_size, seen.st_mtime_ns) or \
                    (there.st_dev, there.st_ino) != (seen.st_dev, seen.st_ino) or holders(path):
                os.unlink(temp)
                return Result(0, 0, busy=True)
            os.replace(temp, path)
        except OSError as e:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise WriteFailed("%s: %s; %s" % (path, e, left)) from e
        # A writer that held the old inode got bytes in during the rename:
        # they are on the handle still open here, and go after the masked bytes.
        late = f.read()
    try:
        if late:
            tail, c, pc = masked(late, pairs, pats)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                write_all(fd, tail)
                os.fsync(fd)
            finally:
                os.close(fd)
            count, pcount = count + c, pcount + pc
        dirfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError as e:
        raise WriteFailed("%s: masked, then %s; %s" % (
            path, e, "%d appended byte(s) may be missing" % len(late) if late else "the rename may not be durable")) from e
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
    print("== would mask %d your-key value(s) + %d pattern match(es) in %d file(s); %d busy"
          " (dry run; add --apply)" % (total, ptotal, files, busy))
    return 2 if (total or ptotal) else 0


if __name__ == "__main__":
    sys.exit(main())
