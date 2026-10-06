"""Keep what each call sent and got back, so the experiment can be rerun and
relabelled later from stored data. Never raises.

The ledger (`jev_meter`) and the spans (`jev_trace`) hold identifiers and
counts by design. A question about Jev that nobody has thought of yet --
a new success criterion, a new label, a second model asked the same thing --
needs the request and the response themselves. This module keeps them: one
JSON line per call per arm, appended to one file per UTC day.

**Where.** `JEV_CORPUS_DIR`, default `~/.local/share/sd/jev-corpus`. The
folder is made 0700 and each file 0600, because a record holds the state as
sent: paths, diffs, subjects. Keep it on the system disk; a volume mounted
`noowners` ignores both modes. Nothing here is ever committed.

**Switching it off.** `JEV_CORPUS=0` (or `off`, `false`, `no`, `disabled`)
stores nothing. Unset means on, the default the meter uses.

**What a caller pays.** One append after the answer is printed. A folder
that cannot be made, a file that cannot be opened, a disk that is full: each
is the same answer to the caller, which is nothing. Stdlib only, like
`jev.py`.

The record's shape belongs to `jev.py`, which builds it; this module stamps
`schema`, `id` and `time` and writes it down. `jev.py` also applies the one
rule about what may not be stored (`CORPUS_HASHED_STAGES`) before it gets
here.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

#: The words that switch the corpus off, the same set the meter accepts.
OFF = ("0", "off", "false", "no", "disabled")

#: The record shape's version. Raise it when a field changes meaning, so a
#: reader can tell old lines from new ones.
SCHEMA = 1

#: What `append` returns. Only `written` means a line exists.
WRITTEN = "written"
SWITCHED_OFF = "switched off"
FAILED = "the corpus did not take the record"


def switched_on(env) -> bool:
    return (env.get("JEV_CORPUS") or "").strip().lower() not in OFF


def directory(env) -> str:
    """`JEV_CORPUS_DIR`, else `~/.local/share/sd/jev-corpus` under `HOME`."""
    named = (env.get("JEV_CORPUS_DIR") or "").strip()
    if named:
        return named
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "sd", "jev-corpus")


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
        path = os.path.join(folder, now.strftime("%Y-%m-%d") + ".jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            # ponytail: one write per line, so the arms' child and `jev`
            # appending at once do not interleave on a local disk; add a
            # `flock` if the folder ever moves to a network share.
            os.write(fd, data)
        finally:
            os.close(fd)
        return WRITTEN
    except Exception:
        # Bare `Exception`, as in `jev_meter.record`: the corpus is
        # bookkeeping, and bookkeeping may never be the reason a judgment fails.
        return FAILED
