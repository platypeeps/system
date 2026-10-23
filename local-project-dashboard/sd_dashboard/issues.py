"""Your issues (sd:4628): each repo's Jira and GitHub issues assigned to or filed by the operator, on Today (sd:5645).

The rows are `sd_db.repos.mine_issues` (pack sd:4636): the open `shadow` issues whose tracker and repo match a repo's issue
sources (`jira=KEY`, `github=owner/name`, sd:3461) and whose `reason` is `assigned` or `filed`. Watched rows, and rows
synced before the reason was stored, stay synced and are not listed here. A read that fails is said, never an empty list:
a pack without `mine_issues`, and a database not yet migrated to the `reason` column, each name their remedy.

Issue intake (sd:4840) is Today's second list: each repo with `github_issues` on, its open issues someone else opened that
no sd item carries yet, from `sd_db.repos.intake_issues` (pack sd:4880). Each row names the ref an item files it under,
`github:<owner>/<repo>#N`, which Today's File as item posts and `sd task add --ref` takes.

Details (sd:5877): each row of both lists also carries the `shadow` columns its Details show (`FACTS`, each None when
the sync did not store it) and the item filed under its ref, read from what `sd shadow sync` stored; no tracker is called.
A read of them that fails is the row's `unread`, so the page says the facts are unknown, never empty.

sd:5899: Your issues is one Jira and GitHub list. `with_github` adds the GitHub issues the Pull requests view used to
list: each issue `today_github.document` holds open, or closed in its window, that is the operator's (`mine`) and no
issue source already lists. A closed row carries its `state`; the page shows it under its closed filter only.
"""

from __future__ import annotations

import sqlite3

from sd_db import repos, writes
from sd_db.errors import SdDbError

TRACKER = {"github": "GitHub", "jira": "Jira"}
#: Each Details fact (sd:5877) to the `shadow` column that holds it. The sync stores no labels, priority or description.
FACTS = {"slug": "repo", "state": "state", "status": "status", "assignee": "assignee", "reporter": "author",
         "created_at": "created_at", "updated_at": "updated_at"}
#: The source of an item filed under a tracker ref (`review.REF_SOURCE`).
REF_SOURCE = "task-ref"


def fallback(entry: dict, row: dict) -> str:
    """A row's key when no ref is known: its tracker, repository path and reference, never the stored address."""
    return f"{row['tracker']}:{entry['repo_path']}{entry['ref']}"


def _details(connection: sqlite3.Connection, pairs: list[tuple[dict, dict]]) -> None:
    """Add to each listed entry its stored facts and the item filed under its ref, read by its pack row's tracker and
    url; a read that fails leaves every fact None and `unread` naming why."""
    try:
        for entry, row in pairs:
            found = connection.execute("SELECT * FROM shadow WHERE tracker = ? AND url = ?", (row["tracker"], row["url"])).fetchone()
            stored = dict(found) if found is not None else {}
            entry.update({name: stored.get(column) for name, column in FACTS.items()}, unread="")
            slug = entry["slug"] or row.get("repo")
            ref = (f"jira:{entry['ref']}" if row["tracker"] == "jira" else
                   f"github:{slug}#{row['number']}" if slug and row["number"] is not None else None)
            held = None
            for key in dict.fromkeys((ref, ref.lower())) if ref else ():
                held = held or writes.item_by_external(connection, REF_SOURCE, key)
            entry["item"] = held["id"] if held is not None else None
            entry.setdefault("key", ref or fallback(entry, row))
    except (SdDbError, sqlite3.Error) as failure:
        for entry, row in pairs:
            entry.update({name: None for name in FACTS}, item=None, unread=str(failure) or "the stored issue was not read")
            entry.setdefault("key", fallback(entry, row))


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
    issues, pairs = [], []
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
            pairs.append((issues[-1], row))
    _details(connection, pairs)
    return {"issues": issues, "issues_error": ""}


def with_github(document: dict, github: dict) -> dict:
    """`document` with the operator's GitHub issues from `github` (`today_github.document`) that it does not list, by
    ref; `github_error` names a GitHub read that failed, so the list never looks complete when it is not."""
    if github.get("error"):
        return {**document, "github_error": github["error"]}
    listed = {str(row.get("key", "")).lower() for row in document["issues"]}
    extra = []
    for entry in (*github["issues"], *(row for row in github["settled"] if row["kind"] == "issue")):
        key = f"github:{entry['ref']}"
        if not entry.get("mine") or key.lower() in listed:
            continue
        listed.add(key.lower())
        extra.append({"repo": entry["repo"], "repo_path": None, "tracker": "GitHub", "ref": f"#{entry['number']}", "key": key,
                      "title": entry["title"], "reason": entry["why"], "last_seen": None, "url": entry["url"],
                      "slug": entry["repo"], "state": entry["state"], "status": None, "assignee": entry.get("assignee"),
                      "reporter": entry.get("author"), "created_at": entry.get("created_at"),
                      "updated_at": entry.get("updated_at"), "item": entry["item"], "unread": ""})
    return {**document, "issues": [*document["issues"], *extra], "github_error": ""}


def intake(connection: sqlite3.Connection) -> dict:
    """`intake`, one object per open third-party issue no item carries, and `intake_error`, why none could be read or ""."""
    from .screens import _repo_labels

    read = getattr(repos, "intake_issues", None)
    if read is None:
        return {"intake": [], "intake_error": "this sd_db has no repos.intake_issues; update the command pack"}
    try:
        found = read(connection)
    except sqlite3.OperationalError as failure:
        reason = str(failure) or "the issue intake could not be read"
        if "no such column: github_issues" in reason:
            reason += "; run sd-db migrate"
        return {"intake": [], "intake_error": reason}
    except (SdDbError, sqlite3.Error) as failure:
        return {"intake": [], "intake_error": str(failure) or "the issue intake could not be read"}
    label = _repo_labels(found)
    rows, pairs = [], []
    for path, found_rows in found.items():
        for row in found_rows:
            url = row["url"] or ""
            rows.append({
                "repo": label(path), "repo_path": path, "ref": f"#{row['number']}",
                "key": f"github:{row['repo']}#{row['number']}", "title": row["title"] or "",
                "author": row["author"] or "", "last_seen": row["last_seen"],
                "url": url if url.startswith("https://") else None,
            })
            pairs.append((rows[-1], row))
    _details(connection, pairs)
    return {"intake": rows, "intake_error": ""}
