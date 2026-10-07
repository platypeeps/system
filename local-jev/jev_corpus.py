"""Keep what each call sent and got back, so the experiment can be rerun and
relabelled later from stored data. Never raises.

The ledger (`jev_meter`) and the spans (`jev_trace`) hold identifiers and
counts by design. A question about Jev that nobody has thought of yet --
a new success criterion, a new label, a second model asked the same thing --
needs the request and the response themselves. This module keeps them: one
JSON line per call per arm, appended to one file per UTC day.

**Where.** `JEV_CORPUS_DIR`, default `~/.local/share/sd/jev-corpus`. The
folder is made 0700 and each file 0600, because a record holds the state as
sent: paths, diffs, subjects. An existing folder or file is held to the
same modes, and a symlink, a FIFO or another user's file is refused. Keep
it on the system disk; a volume mounted `noowners` ignores both modes.
Nothing here is ever committed.

**Switching it off.** `JEV_CORPUS=0` (or `off`, `false`, `no`, `disabled`)
stores nothing. Unset means on, the default the meter uses.

**How long.** A day's first line removes the day files more than
`JEV_CORPUS_DAYS` UTC days older than today's (default `KEEP_DAYS`), so the
sweep runs once a day on the append path and needs no scheduler.

**What a caller pays.** One append after the answer is printed. A folder
that cannot be made, a file that cannot be opened, a lock held past
`LOCK_WAIT`, a disk that is full: each is the same answer to the caller,
which is nothing. Stdlib only, like `jev.py`.

The record's shape belongs to `jev.py`, which builds it; this module stamps
`schema`, `id` and `time` and writes it down. `jev.py` also applies the
rules about what may not be stored as text (`CORPUS_HASHED_STAGES`, and the
redaction of every other record) before it gets here.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import uuid
from datetime import datetime, timedelta, timezone

#: The words that switch the corpus off, the same set the meter accepts.
OFF = ("0", "off", "false", "no", "disabled")

#: The record shape's version. Raise it when a field changes meaning, so a
#: reader can tell old lines from new ones.
SCHEMA = 1

#: What `append` returns. Only `written` means a line exists.
WRITTEN = "written"
SWITCHED_OFF = "switched off"
FAILED = "the corpus did not take the record"

#: How long an append waits for another writer's lock before it drops the
#: record, and how often it tries. A held lock may never hold up a call.
LOCK_WAIT = 1.0
LOCK_TRY = 0.05

#: How many UTC days before today's the corpus keeps when `JEV_CORPUS_DAYS`
#: names no positive whole number.
KEEP_DAYS = 30


def switched_on(env) -> bool:
    return (env.get("JEV_CORPUS") or "").strip().lower() not in OFF


def directory(env) -> str:
    """`JEV_CORPUS_DIR`, else `~/.local/share/sd/jev-corpus` under `HOME`."""
    named = (env.get("JEV_CORPUS_DIR") or "").strip()
    if named:
        return named
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "sd", "jev-corpus")


def keep_days(env) -> int:
    """`JEV_CORPUS_DAYS` when it is a positive whole number, else `KEEP_DAYS`."""
    try:
        days = int((env.get("JEV_CORPUS_DAYS") or "").strip())
    except ValueError:
        return KEEP_DAYS
    return days if days > 0 else KEEP_DAYS


def sweep(folder: str, today, env) -> None:
    """Remove the day files more than `keep_days` before `today`. Only this
    user's regular files named `YYYY-MM-DD.jsonl` go; anything else stays."""
    oldest = (today - timedelta(days=keep_days(env))).strftime("%Y-%m-%d")
    for name in os.listdir(folder):
        day, _, rest = name.partition(".")
        if rest != "jsonl" or day >= oldest:
            continue
        try:
            # `strptime` also takes `2025-1-2`; only the name `append` writes
            # may go.
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                continue
            path = os.path.join(folder, name)
            if private(os.lstat(path), stat.S_ISREG):
                os.unlink(path)
        except (ValueError, OSError):
            continue


def private(info, kind) -> bool:
    """Whether a path is of `kind`, not a symlink, and this user's own."""
    return kind(info.st_mode) and info.st_uid == os.getuid()


def locked(fd) -> bool:
    """Take the file's lock within `LOCK_WAIT`, or report that it is held."""
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_TRY)


def whole_lines(fd, size: int) -> int:
    """The length of the file up to and including its last newline."""
    end = size
    while end > 0:
        chunk = os.pread(fd, min(4096, end), end - min(4096, end))
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            return end - len(chunk) + cut + 1
        end -= len(chunk)
    return 0


def append(record: dict, env=None) -> str:
    """Append one record as a JSON line. Never raises; the return is for the
    suite."""
    env = os.environ if env is None else env
    try:
        if not switched_on(env):
            return SWITCHED_OFF
        now = datetime.now(timezone.utc)
        line = dict(record, schema=SCHEMA, id=uuid.uuid4().hex,
                    time=now.isoformat(timespec="milliseconds"))
        data = (json.dumps(line, sort_keys=True, default=str) + "\n").encode("utf-8")
        folder = directory(env)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        # A folder or file that already exists is held to the same modes, and
        # a symlink or someone else's file is refused: a record holds the
        # state as sent.
        if not private(os.lstat(folder), stat.S_ISDIR):
            return FAILED
        os.chmod(folder, 0o700)
        path = os.path.join(folder, now.strftime("%Y-%m-%d") + ".jsonl")
        # O_NONBLOCK so a FIFO in the file's place fails here instead of
        # waiting for a reader; the type check below then refuses it.
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_NONBLOCK, 0o600)
        try:
            if not private(os.fstat(fd), stat.S_ISREG):
                return FAILED
            os.fchmod(fd, 0o600)
            # Locked, because `jev` and the arms' child append to the same
            # file, but never waited on for long: a writer that stopped while
            # holding it costs one record, not every later call. A line that
            # could not be written whole is cut off again, so a full disk
            # leaves no fragment for the next line to run into.
            if not locked(fd):
                return FAILED
            # A writer killed mid-line left a tail with no newline; cut it, or
            # this line would be glued to it and both lost to a reader.
            start = whole_lines(fd, os.fstat(fd).st_size)
            os.ftruncate(fd, start)
            try:
                done = 0
                while done < len(data):
                    wrote = os.write(fd, data[done:])
                    if wrote <= 0:
                        raise OSError("no progress")
                    done += wrote
            except OSError:
                os.ftruncate(fd, start)
                return FAILED
        finally:
            os.close(fd)
        # The day's first line: the one append a day that sweeps old days. A
        # sweep that fails, or a bound past the calendar, keeps every day and
        # leaves the line written.
        if start == 0:
            try:
                sweep(folder, now, env)
            except Exception:
                pass
        return WRITTEN
    except Exception:
        # Bare `Exception`, as in `jev_meter.record`: the corpus is
        # bookkeeping, and bookkeeping may never be the reason a judgment fails.
        return FAILED
