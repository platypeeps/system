"""A database with rows on it, for the three screens to render.

The harness proper is `sd_db.testing` and this is not a second one: it seeds
*rows*, which is what these tests are about. Anything that doubles the outside
world -- GitHub, launchd, a home directory -- comes from `sd_db.testing`.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sd_db import (
    add_note,
    connect,
    create_assignment,
    create_item,
    record_cost,
    transition,
    update_assignment,
    upsert_repo,
    upsert_shadow,
)
from sd_db.migrate import initialise

NOW = "2026-09-06T12:00:00Z"


class ScreenCase(unittest.TestCase):
    """A fixture database, and the clock frozen at `NOW`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sd.db"
        initialise(self.path)
        self.connection = connect(self.path)
        self.addCleanup(self.connection.close)
        self.now = NOW

    # -- seeds -------------------------------------------------------------

    def repo(self, path: str = "/repos/system") -> str:
        upsert_repo(self.connection, path, remote="git@example.invalid:x.git")
        return path

    def item(self, title: str = "an item", **columns) -> int:
        columns.setdefault("kind", "work")
        return create_item(self.connection, title=title, **columns)

    def note(self, item: int, body: str, kind: str = "followup", **columns) -> int:
        return add_note(self.connection, item, kind=kind, body=body, **columns)

    def age(self, item: int, days: int) -> None:
        """Backdate the item's status history so the row reads as `days` old.

        `status_since` is the newest `status_change` note, and `create_item`
        writes one at the real clock -- so every freshly seeded row is zero
        days old and every one of them lands in the first age bucket. A test
        about buckets needs rows in more than one of them.

        The library has no setter for a note's timestamp, correctly: nothing in
        production backdates history. So the fixture writes the column, which
        is seeding and not a second writer -- it touches `note.timestamp`, and
        never `item.status`.
        """
        when = (
            datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.connection.execute(
            "UPDATE note SET timestamp = ? WHERE item = ? AND kind = 'status_change'",
            (when, item),
        )
        self.connection.commit()

    def provider(self, name: str = "opus", **columns) -> str:
        """A `provider` row. `assignment.provider` is a foreign key into it."""
        self.connection.execute(
            "INSERT OR IGNORE INTO provider (name, enabled, author_rank, reviewer_rank) "
            "VALUES (?, ?, ?, ?)",
            (name, int(columns.get("enabled", 1)), columns.get("author_rank"),
             columns.get("reviewer_rank")),
        )
        self.connection.commit()
        return name

    def assignment(self, item: int, **columns) -> int:
        columns.setdefault("role", "author")
        columns.setdefault("status", "queued")
        if columns.get("provider"):
            self.provider(columns["provider"])
        return create_assignment(self.connection, item=item, **columns)

    def render(self, path: str, parameters=None) -> str:
        from sd_dashboard.server import route

        return route(self.connection, path, parameters or {}, now=self.now)


__all__ = [
    "NOW",
    "ScreenCase",
    "record_cost",
    "transition",
    "update_assignment",
    "upsert_shadow",
]
