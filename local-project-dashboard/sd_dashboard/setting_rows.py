"""Dashboard lists held as workflow database setting rows (sd:5109), each the pipe text its old file held.

`LISTS` names each list's row, its old file under `<config>/project-dashboard/` and what Settings says about it. The
readers (`places.realms`, `reports_screen.families`, `hoa_screen.thresholds`, `health_collectors.storage_list`) keep
their own grammar and read their text through `text`, the one rule:

- **Row wins.** A row the database holds is the answer; a cleared row (`None`) is "not configured", and the old file is
  not read again.
- The old file answers only for a list the database has never held, so the first Save on Settings is the import.
- A failed row read is unknown: `text` names it, and no file is read in its place.

`digest` is what a Settings save's `before` must equal: the source and text the page showed (as Home's tiles, sd:3987).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

__all__ = ["LISTS", "current", "digest", "text"]

#: Each list's row: its old file's name and what Settings says it holds.
LISTS = {
    "dashboard.realms": ("realms.conf", "Tasks' and Writing's realm views, one realm|<id>|<label>|<glob> line each. "
                         "A task is in the first realm whose glob matches its repo path (* crosses folders, ~ is home); "
                         "no match is Personal. Ids are lower case with hyphens, not personal, hoa, home or all."),
    "dashboard.report_families": ("report-families.conf", "Reports' job families, one "
                                  "family|<key>|<label>|<icon>|<job>,<job>,... line each, in the order the page draws "
                                  "them. Families hoa and home are the HOA and Home pages' Reports and Briefs."),
    "dashboard.hoa_thresholds": ("hoa-thresholds.conf", "HOA's trend levels, one <series>|below|<number> or "
                                 "<series>|above|<number> line each; series is tank, runtime or aquifer."),
    "dashboard.disk_storage": ("disk.conf", "Health's storage folders, one storage|<path> line each (~ and $HOME "
                               "expand): Disk sizes the folders inside each, the place for large uncommitted data."),
}


def source(key: str) -> str:
    """How a page names the row as a list's source."""
    return f"workflow database setting {key}"


def current(connection, key: str, legacy: Path) -> tuple[str, str | None]:
    """`(origin, text)`: `database` (a cleared row holds None), else `file`, else `missing`.

    Raises `LookupError` naming why the row was not read, and `OSError` or `UnicodeDecodeError` for an unreadable file.
    """
    from sd_db import writes
    from sd_db.errors import SdDbError

    if connection is None:
        raise LookupError("no workflow database connection")
    try:
        held = writes.read_settings(connection, [key])
    except (SdDbError, sqlite3.Error) as error:
        raise LookupError(f"{error.__class__.__name__}: {error}") from None
    if key in held:
        return "database", held[key]
    try:
        return "file", legacy.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None


def text(connection, key: str, legacy: Path) -> tuple[str | None, str, str]:
    """`(text, source, problem)`: the list's text or None when it holds none, where it came from, and why it was not
    read ("" when it was). A problem means unknown: the caller says "not read", never "not configured"."""
    named = f"<config>/project-dashboard/{legacy.name}"
    try:
        origin, found = current(connection, key, legacy)
    except LookupError as unread:
        return None, source(key), f"not read: {unread}"
    except (OSError, UnicodeDecodeError) as error:
        return None, named, f"not read: {error.__class__.__name__}"
    return found, source(key) if origin == "database" else named, ""


def digest(origin: str, found: str | None) -> str:
    """What a save's `before` must equal: the text the page showed, and where it came from."""
    return hashlib.sha256(f"{origin}\n{found or ''}".encode("utf-8")).hexdigest()
