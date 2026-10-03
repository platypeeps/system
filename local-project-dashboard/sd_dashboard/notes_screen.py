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
network call and a credential; the daily note text has no reader. Quick
notes have no store kind yet (sd:2120 records the decision), so the page
keeps them for the visit only and says so.

A source that raises is named in `sources` with its reason and adds no rows,
so a failed read never looks like a quiet day. Nothing here writes.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from sd_db import reads
from sd_db.errors import SdDbError

from . import activity_screen

__all__ = ["DAYS", "UNKNOWN", "document"]

#: Days the document carries, today included.
DAYS = 7

UNKNOWN = {
    "mail": "No collector reads mail: brief and status mail stay in Gmail, and reading it needs a network call and a credential.",
    "daily": "No collector reads the daily note: the vault's daily notes are not indexed for this page, so the body is not shown rather than guessed.",
    "quick": "sd store has no loose-note kind, so a quick note is kept on this page until you leave it; declaring one is a pack change.",
}

_MOVE = re.compile(r"^(\S+) -> (\S+) by (.+?)(?::|$)")
FAILURES = (SdDbError, sqlite3.Error, OSError, ValueError)


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
