"""Snooze: a Today or Health row hidden until a time (sd:1896).

A row's key is its page and its own id, `today:job:nightly:1` or
`health:br:system`, so one page's snooze never hides the other's row. The
ids key on the fact that changed, as `now_screen` says: a job that fails with
another exit code, or a repository with one more unpushed commit, is a new row
that no older snooze covers.

- `split` is what `now_screen.document` and `health_screen.document` call: the
  rows shown, and the snoozed ones, each with `until`. `sd_db.writes.snoozed`
  is read once per document. A read that fails hides nothing and says why: an
  unread snooze is not a snooze.
"""

from __future__ import annotations

import sqlite3

from sd_db import writes
from sd_db.errors import SdDbError

__all__ = ["PAGES", "held", "key", "split"]

PAGES = ("today", "health")


def key(page: str, row: str) -> str:
    return f"{page}:{row}"


def held(connection, *, now: str) -> tuple[dict[str, str], str]:
    """Every key snoozed at `now`, and the reason when they could not be read."""
    try:
        return writes.snoozed(connection, now=now), ""
    except (SdDbError, sqlite3.Error) as failure:
        return {}, str(failure) or "the snoozes could not be read"


def split(page: str, rows: list[dict], snoozes: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """The rows shown, and the rows snoozed with the time each shows again."""
    shown, hidden = [], []
    for row in rows:
        until = snoozes.get(key(page, row.get("id", "")))
        if until:
            hidden.append({**row, "until": until})
        else:
            shown.append(row)
    return shown, hidden

