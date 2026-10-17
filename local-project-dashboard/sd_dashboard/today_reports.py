"""Briefs and problem reports on Today: what the reports view used to push by ntfy or mail (operator ruling 2026-10-17).

Two lists, read from the workflow database and from nothing else:

- `problems`: every open report that asks for a person. A report row (kind `report`, any source but `brief`) that is not
  `done` and whose `fields.attention` is not false: a non-zero exit, an `SD_REPORT_ATTENTION:` line, or fields nobody can
  read, which is for a person to look at as well (`reporting.UNREADABLE_FIELDS`). A quiet run report is left out, as the
  backlog leaves it out. Acknowledge posts the Reports page's route, `/api/reports/<id>/acknowledge`, with the revision.
- `briefs`: the routine briefs (`briefs_screen`) filed in the last `WINDOW_DAYS` days, newest first, that the operator has
  not marked seen. Seen is a Today snooze (`snooze`), key `today:brief:<stem>`, held until `SEEN_DAYS` days after the
  brief's day: by then the brief has aged off Today, and the Briefs page keeps it. A brief whose text grows (an `extend`)
  reads as new again, since its word count is its problem. No new store: the snooze is the existing one.

A read that fails is its reason (`problems_error`, `briefs_error`), never an empty list. Nothing here writes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta, timezone

from sd_db import reads, reporting
from sd_db.errors import SdDbError

from . import briefs_screen, snooze

__all__ = ["SEEN_DAYS", "SHOWN", "WINDOW_DAYS", "document"]

#: Briefs older than this many days stay on the Briefs page only.
WINDOW_DAYS = 7
#: How long Seen holds a brief off Today, counted from its day: past the window, and inside the snooze's 31-day limit.
SEEN_DAYS = WINDOW_DAYS + 1
#: Rows shown per list; the rest are counted and stay on Reports or Briefs.
SHOWN = 50
#: The last characters of a report's body Details shows.
BODY_CHARS = 2000

_OPEN = ("FROM item WHERE kind = 'report' AND source IS NOT 'brief' AND status != 'done'"
         " AND (CASE WHEN json_valid(fields) THEN json_type(fields, '$.attention') END) IS NOT 'false'")


def _problem(connection, row, revision: str) -> dict:
    try:
        fields = reporting.fields_document(connection, row["fields"])
        readable = isinstance(fields, dict)
    except reporting.UnreadableFields:
        fields, readable = {}, False
    fields = fields if readable else {}
    report = fields.get("report") if isinstance(fields.get("report"), dict) else {}
    repeats = fields.get("repeats") if isinstance(fields.get("repeats"), dict) else {}
    basis = repeats.get("basis") if repeats.get("basis") is not None else report.get("attention_basis")
    basis = basis if isinstance(basis, str) else ""
    try:
        body = json.loads(row["body"] or "{}")
    except ValueError:
        body = {}
    text = body.get("text") if isinstance(body, dict) and isinstance(body.get("text"), str) else ""
    return {"id": f"report:{row['id']}", "item": row["id"], "title": row["title"], "at": row["created_at"],
            "job": report.get("job") or None, "exit": report.get("exit_code"), "host": report.get("host") or None,
            "why": basis.splitlines()[0] if basis else ("its fields cannot be read" if not readable else ""),
            "basis": basis, "repeats": repeats.get("count"), "body": text[-BODY_CHARS:], "cut": len(text) > BODY_CHARS,
            "revision": revision, "fields_read": readable}


def problems(connection: sqlite3.Connection) -> tuple[list[dict], int]:
    """The open reports that ask for a person, newest first and at most `SHOWN`, and how many more there are."""
    rows = connection.execute(f"SELECT id, title, created_at, fields, body {_OPEN} ORDER BY created_at DESC, id DESC LIMIT ?",
                              (SHOWN,)).fetchall()
    revisions = reads.revisions(connection, [row["id"] for row in rows])
    more = connection.execute(f"SELECT count(*) {_OPEN}").fetchone()[0] - len(rows)
    return [_problem(connection, row, revisions[row["id"]]) for row in rows], more


def _seen_until(day: str) -> str:
    moment = datetime.combine(datetime.fromisoformat(day).date() + timedelta(days=SEEN_DAYS), time(12), timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def briefs(connection: sqlite3.Connection, *, now: str, snoozes: dict) -> tuple[list[dict], int, int]:
    """The briefs of the last `WINDOW_DAYS` days not marked seen, newest first; how many more; how many rows were bad."""
    today = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    got = briefs_screen._library().rows(connection, since=since, limit=briefs_screen.ROWS + 1)
    if not isinstance(got, list):
        raise ValueError("the brief reader returned no list")  # noqa: TRY004 - external document validation
    found, bad = briefs_screen.rows(got, None, briefs_screen.copies(None), None)
    rows = [{**row, "id": f"brief:{row['id']}", "stem": row["id"], "until": _seen_until(row["day"]),
             "problem": [row["id"], row["words"]]} for row in found if row["day"] >= since]
    shown, _seen = snooze.split("today", rows, snoozes)
    return shown[:SHOWN], max(0, len(shown) - SHOWN), bad


def document(connection: sqlite3.Connection, *, now: str, snoozes: dict) -> dict:
    """`problems`, `problems_more`, `problems_error`; `briefs`, `briefs_more`, `briefs_error`, `briefs_skipped`."""
    out = {"problems": [], "problems_more": 0, "problems_error": "",
           "briefs": [], "briefs_more": 0, "briefs_error": "", "briefs_skipped": 0}
    try:
        out["problems"], out["problems_more"] = problems(connection)
    except (SdDbError, sqlite3.Error, ValueError, KeyError) as failure:
        out["problems_error"] = str(failure) or "the reports could not be read"
    try:
        out["briefs"], out["briefs_more"], out["briefs_skipped"] = briefs(connection, now=now, snoozes=snoozes)
    except (SdDbError, sqlite3.Error, ValueError, KeyError, TypeError) as failure:
        out["briefs_error"] = str(failure) or "the briefs could not be read"
    return out
