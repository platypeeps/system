"""Snooze: a Today or Health row hidden until a time (sd:1896).

A row's key is its page and its own id, `today:job:nightly:1` or
`health:br:system`, so one page's snooze never hides the other's row. Most
Health ids name a resource, not its problem: `dep:<repo>` is the same id with
one low alert or ten critical ones. So a snooze also holds `seen`, the
`fingerprint` of the row the operator snoozed, and hides the row only while
it still reads the same: a changed or worsened problem shows again.

- `fingerprint` hashes the row's `state` and `rank` with its `what`, `detail`
  and `list`. A row whose text carries a measure that drifts with the clock or
  the disk (days to an expiry, a percent used, a log time) names its problem
  in `problem` instead, and the hash reads that; `split` drops `problem`.

- A work item (sd:3271) is a Today row too: id `item:<id>`, so its key is `today:item:<id>`, and its problem is its
  status and due date (`now_screen.work_rows`). A new title does not show it again; a new status or due date does.

- `split` is what `now_screen.document` and `health_screen.document` call: the
  rows shown, and the snoozed ones, each with `until`; every row carries its
  `seen`. `sd_db.writes.snoozed` is read once per document. A read that fails
  hides nothing and says why: an unread snooze is not a snooze.
- `request` is `POST /api/snooze`, `{"page", "row", "until", "seen"}`, which
  both pages post: `until` is an aware ISO time, or null to show the row
  again, and `seen` is the row's own, as the page read it.
  `sd_db.writes.snooze` writes it through `record_state` and judges the time.
  The row need not be listed now: a key no row has hides nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from sd_db import writes
from sd_db.errors import SdDbError

__all__ = ["PAGES", "fingerprint", "held", "key", "request", "split"]

PAGES = ("today", "health")


def key(page: str, row: str) -> str:
    return f"{page}:{row}"


def fingerprint(row: dict) -> str:
    """What the operator saw of the row's problem, as 16 hex digits."""
    seen = [row.get("state"), row.get("rank"), row.get("problem", [row.get("what"), row.get("detail"), row.get("list")])]
    return hashlib.sha256(json.dumps(seen, sort_keys=True, default=str).encode()).hexdigest()[:16]


def held(connection, *, now: str) -> tuple[dict[str, dict[str, str]], str]:
    """Every key snoozed at `now`, and the reason when they could not be read."""
    try:
        return writes.snoozed(connection, now=now), ""
    except (SdDbError, sqlite3.Error) as failure:
        return {}, str(failure) or "the snoozes could not be read"


def split(page: str, rows: list[dict], snoozes: dict[str, dict[str, str]]) -> tuple[list[dict], list[dict]]:
    """The rows shown, and the rows snoozed with the time each shows again; a row whose problem changed shows."""
    shown, hidden = [], []
    for row in rows:
        seen = fingerprint(row)
        row = {**{name: value for name, value in row.items() if name != "problem"}, "seen": seen}
        snoozed = snoozes.get(key(page, row.get("id", "")))
        if snoozed and snoozed["seen"] == row["seen"]:
            hidden.append({**row, "until": snoozed["until"]})
        else:
            shown.append(row)
    return shown, hidden


def request(payload: dict):
    """The POST's checks, before the server opens a writer; the write as `action_route` returns one."""
    if (set(payload) != {"page", "row", "until", "seen"} or payload["page"] not in PAGES
            or not isinstance(payload["row"], str) or not payload["row"].strip()
            or not (payload["until"] is None or isinstance(payload["until"], str))
            or not (payload["seen"] is None or isinstance(payload["seen"], str))):
        raise ValueError("Send the page (today or health), the row id, the time it shows again or null to show it now, "
                         "and the row's seen.")
    target = key(payload["page"], payload["row"])

    def write(connection):
        writes.snooze(connection, target, payload["until"], seen=payload["seen"])
        return {"key": target, "until": payload["until"] and writes.stamp(payload["until"])}

    return write
