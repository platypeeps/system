"""Notes: what happened each day, behind the default UI's Notes page (sd:2120).

The page is `v2/notes.html`; it holds no rows. It reads one JSON document,
`/api/notes`, built here for the last `DAYS` days, each a day in this
machine's own time zone, from records the library already keeps:

- **merges**: the delivery notes `sd-ship` writes when it lands a pull
  request, as the Activity page reads them (`activity_screen.merges`). The
  design walked each checkout's first-parent history; a delivery note is the
  same merge, recorded once, with no git walk.
- **done** and **opened**: the `status_change` notes of the day
  (`reads.status_change_notes`): a move to `done`, and `opened as`.
- **runs**: runner assignments that started or ended that day, as Activity
  reads them (`activity_screen.runs`).

The design's other sources have no reader, and each is in `unknown` with the
reason, so the page draws them hatched: unknown is not zero. Mail needs a
network call and a credential; the daily note text has no reader.

A source that raises is named in `sources` with its reason and adds no rows,
so a failed read never looks like a quiet day. The document writes nothing.

Quick notes (sd:2549) are `sdw.quick-note` items in the vault, a kind
sd-writing-pack declares (sd:2196). `quick_notes` lists them through `sd store
list`, and `quick_add` keeps one through `sd store add`, each `sd` from PATH
with an argv list and no shell. sd does not enforce the kind's rules, so
`quick_text` does (operator ruling on sd:2549): non-empty, one line, no other
control character, no double quote, at most `QUICK_MAX` characters. The title
is the local time, `YYYY-MM-DD HHMMSS`; sd refuses a title taken anywhere in
the vault, and the next try adds ` 2`, ` 3` and so on.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import unicodedata
from datetime import date, datetime, time, timedelta, timezone

from sd_db import reads
from sd_db.errors import SdDbError

from . import activity_screen

__all__ = ["DAYS", "QUICK_KIND", "QUICK_MAX", "KindMissing", "UNKNOWN", "document", "quick_add", "quick_notes", "quick_text"]

#: Days the document carries, today included.
DAYS = 7

UNKNOWN = {
    "mail": "No collector reads mail: brief and status mail stay in Gmail, and reading it needs a network call and a credential.",
    "daily": "No collector reads the daily note: the vault's daily notes are not indexed for this page, so the body is not shown rather than guessed.",
}

_MOVE = re.compile(r"^(\S+) -> (\S+) by (.+?)(?::|$)")
FAILURES = (SdDbError, sqlite3.Error, OSError, ValueError)

#: The store kind a quick note is kept as.
QUICK_KIND = "sdw.quick-note"
#: The longest quick note, in characters, once newlines are spaces: a thought, a link or a name, not a page.
QUICK_MAX = 1000
#: One `sd store` call's ceiling; `add` walks the whole vault for a title collision.
SD_SECONDS = 30
#: Titles one keep tries: the timestamp, then the suffixes ` 2` to ` 9` for notes kept in the same second.
QUICK_TRIES = 9
#: What sd says when the plugin or its kind is not installed here.
_NO_KIND = re.compile(r"declares no kind|no registered plugin has prefix")


class KindMissing(ValueError):
    """`sdw.quick-note` is not installed on this machine; the page says so instead of failing."""


def _sd(*arguments: str) -> str:
    """`sd store <arguments>`'s stdout; a failure is a ValueError carrying sd's own last line."""
    try:
        done = subprocess.run(["sd", "store", *arguments], capture_output=True, text=True, timeout=SD_SECONDS, check=False)
    except FileNotFoundError:
        raise ValueError("sd is not on the dashboard's PATH, so quick notes cannot be read or kept") from None
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"sd store could not finish: {error}") from None
    if done.returncode:
        said = (done.stderr.strip().splitlines() or [f"sd store exited {done.returncode}"])[-1]
        if _NO_KIND.search(said):
            raise KindMissing(f"{QUICK_KIND} is not installed on this machine: {said}")
        raise ValueError(said)
    return done.stdout


def quick_text(raw) -> str:
    """The note as `sd store add` gets it: newlines flattened to spaces and the ends trimmed; ValueError says which rule failed."""
    if not isinstance(raw, str):
        raise ValueError("A quick note is text.")
    text = re.sub(r"\r\n|[\r\n\u2028\u2029]", " ", raw).strip()
    if not text:
        raise ValueError("The quick note is empty.")
    if any(unicodedata.category(c) == "Cc" for c in text):
        raise ValueError("A quick note holds no control character, such as a tab.")
    if '"' in text:
        raise ValueError('A quick note holds no double quote (").')
    if len(text) > QUICK_MAX:
        raise ValueError(f"A quick note holds at most {QUICK_MAX} characters; this one has {len(text)}.")
    return text


def quick_notes() -> list[dict]:
    """Every `sdw.quick-note`, newest title first; KindMissing when the kind is not installed."""
    try:
        out = _sd("list", QUICK_KIND, "--json")
    except KindMissing:
        raise
    except ValueError as error:
        # `sd store add` makes the kind's folder; until the first note is kept, `list` refuses the folder as missing.
        if "the vault does not hold this kind" in str(error):
            return []
        raise
    rows = json.loads(out)
    if not isinstance(rows, list):
        raise ValueError(f"sd store list {QUICK_KIND} did not return a list")
    notes = [{"text": str(row.get("text") or ""), "title": str(row.get("title") or "")} for row in rows if isinstance(row, dict)]
    return sorted(notes, key=lambda note: note["title"], reverse=True)


def quick_add(text: str) -> dict:
    """Keep `text`, already passed through `quick_text`, under the first free timestamp title; returns the title and text."""
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    for n in range(1, QUICK_TRIES + 1):
        title = stamp if n == 1 else f"{stamp} {n}"
        try:
            _sd("add", QUICK_KIND, title, "--field", f"text={text}")
        except KindMissing:
            raise
        except ValueError as error:
            if "already exists" in str(error):
                continue
            raise
        return {"title": title, "text": text}
    raise ValueError(f"Every title from {stamp} to {stamp} {QUICK_TRIES} is taken; keep the note again.")


def _utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(stamp: str) -> str:
    return _utc(stamp).astimezone().date().isoformat()


def _midnight(day: date) -> datetime:
    """The start of a local day, in UTC; the OS's zone rules give each day its own offset, so DST reads right."""
    return datetime.combine(day, time()).astimezone().astimezone(timezone.utc)


def _changes(connection, start, end):
    done, opened = [], {}
    for row in reads.status_change_notes(connection, since=_iso(start), until=_iso(end)):
        day = _local_day(row["timestamp"])
        if row["body"].startswith("opened as "):
            opened[day] = opened.get(day, 0) + 1
            continue
        move = _MOVE.match(row["body"])
        if move and move.group(2) == "done":
            done.append({"id": f"done:{row['id']}", "day": day, "at": _iso(_utc(row["timestamp"])), "item": row["item"],
                         "title": row["title"], "kind": row["kind"], "repo": activity_screen._repo_label(row["repo"]),
                         "from": move.group(1), "by": move.group(3)})
    return done, opened


def document(connection: sqlite3.Connection, *, now: str) -> dict:
    """The Notes page's document: the last `DAYS` local days, each with its merges, done items, opened count and runs."""
    today = _utc(now).astimezone().date()
    first = today - timedelta(days=DAYS - 1)
    start, end = _midnight(first), _midnight(today + timedelta(days=1))
    days = {(first + timedelta(days=n)).isoformat(): {"merges": [], "done": [], "opened": 0, "runs": []} for n in range(DAYS)}
    sources = {}

    def read(name, fn):
        try:
            return fn()
        except FAILURES as error:
            sources[name] = f"{error.__class__.__name__}: {error}"
            return None

    for row in read("merges", lambda: activity_screen.merges(connection, start, end)) or []:
        days.get(_local_day(row["at"]), {}).setdefault("merges", []).append(
            {k: row[k] for k in ("id", "at", "repo", "what", "ref", "url", "pr", "item", "commit")})
    changes = read("changes", lambda: _changes(connection, start, end))
    if changes:
        for row in changes[0]:
            days.get(row.pop("day"), {}).setdefault("done", []).append(row)
        for day, count in changes[1].items():
            if day in days:
                days[day]["opened"] = count
    for row in read("runs", lambda: activity_screen.runs(connection, start, end)) or []:
        days.get(_local_day(row["at"]), {}).setdefault("runs", []).append(
            {k: row[k] for k in ("id", "at", "s", "repo", "what", "ref", "n", "item", "role", "provider", "status", "started", "ended")})
    return {
        "read": now,
        "today": today.isoformat(),
        "tz": datetime.combine(today, time(12)).astimezone().tzname() or "local",
        "days": days,
        "sources": sources,
        "unknown": dict(UNKNOWN),
    }
