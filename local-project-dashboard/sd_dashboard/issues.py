"""Your issues (sd:4628): each repo's Jira and GitHub issues assigned to or filed by the operator, on Today and Tasks.

The rows are `sd_db.repos.mine_issues` (pack sd:4636): the open `shadow` issues whose tracker and repo match a repo's issue
sources (`jira=KEY`, `github=owner/name`, sd:3461) and whose `reason` is `assigned` or `filed`. Watched rows, and rows
synced before the reason was stored, stay synced and are not listed here. A read that fails is said, never an empty list:
a pack without `mine_issues`, and a database not yet migrated to the `reason` column, each name their remedy.
"""

from __future__ import annotations

import sqlite3

from sd_db import repos
from sd_db.errors import SdDbError

TRACKER = {"github": "GitHub", "jira": "Jira"}


def document(connection: sqlite3.Connection) -> dict:
    """`issues`, one object per listed issue, and `issues_error`, the reason none could be read or ""."""
    from .screens import _repo_labels

    read = getattr(repos, "mine_issues", None)
    if read is None:
        return {"issues": [], "issues_error": "this sd_db has no repos.mine_issues; update the command pack"}
    try:
        found = read(connection)
    except sqlite3.OperationalError as failure:
        reason = str(failure) or "the issues could not be read"
        if "no such column: reason" in reason:
            reason += "; run sd-db migrate"
        return {"issues": [], "issues_error": reason}
    except (SdDbError, sqlite3.Error) as failure:
        return {"issues": [], "issues_error": str(failure) or "the issues could not be read"}
    label = _repo_labels(found)
    issues = []
    for path, rows in found.items():
        for row in rows:
            url = row["url"] or ""
            issues.append({
                "repo": label(path), "repo_path": path, "tracker": TRACKER.get(row["tracker"], row["tracker"]),
                "ref": f"#{row['number']}" if row["number"] is not None else url.rstrip("/").rsplit("/", 1)[-1],
                "title": row["title"] or "", "reason": row["reason"], "last_seen": row["last_seen"],
                # Only a web address is a link; the page draws any other as text.
                "url": url if url.startswith("https://") else None,
            })
    return {"issues": issues, "issues_error": ""}
