"""Health: the rows behind the default UI's Health page (sd:2115).

The page is `v2/health.html`; it holds no rows. It reads one JSON document,
`/api/health`, built here from the readers the dashboard already has, so the
port adds no collector:

- Worktrees: `fleet.collect("sessions")`, the child Operations > Sessions and
  Today's Now read. A registration whose directory is gone is a row per
  checkout, carrying the registrations behind it; one whose files could not
  be read is a row of its own, in the unknown state.
- Attribution: `reads.missing_trailers`, the count Operations > Progress
  shows: commits of the last seven days, in every registered repository,
  with no `Authored-with:` trailer.

The design source shows seven areas. The other five -- Disk, Credentials,
Branches, Dependencies, Security -- and the parts of Worktrees and
Attribution no reader covers are in the document as `missing`, by name, so
the page says what it does not read instead of showing a clean lamp. A
reader that fails is its area's `error`, never an empty area: a fleet
nobody could read must not look like a fleet with nothing wrong.

Nothing here writes. The page's fixes are CLI lines for Copy; no route
prunes a worktree or attributes a commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sd_db import reads
from sd_db.errors import SdDbError

from . import fleet as fleet_module

__all__ = ["AREAS", "document"]

#: The design's areas, in its order: id, name, the reader's source line, and what no reader covers yet.
AREAS = (
    ("disk", "Disk", None,
     ("volume use (df -k)", "repo-storage folder sizes", "build output left in worktrees")),
    ("cred", "Credentials", None,
     ("GitHub PAT presence and expiry", "gh CLI sign-in", "HA_TOKEN test", "MCP server status (claude mcp list)")),
    ("attr", "Attribution", "git log --since=7 days per registered repo · %(trailers)",
     ("counts per repo", "the five-week history", "your commits only: the count covers every author")),
    ("wt", "Worktrees", "the fleet's .git/worktrees registrations",
     ("merged worktrees still on disk (merge-base --is-ancestor)",)),
    ("br", "Branches", None, ("local branches merged into origin's default branch",)),
    ("dep", "Dependencies", None, ("open Dependabot alerts per repo",)),
    ("sec", "Security", None, ("open secret-scanning alerts", "repos with scanning off")),
)


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _worktree_rows(document) -> list[dict]:
    """One row per checkout with registrations whose directory is gone, and per checkout with unreadable ones."""
    trees = document.get("worktrees") if isinstance(document, dict) else None
    root = document.get("root") if isinstance(document, dict) else None
    if not isinstance(trees, list) or not isinstance(root, str) or any(
            not isinstance(tree, dict) or not isinstance(tree.get("state"), str) or not isinstance(tree.get("repo"), str)
            for tree in trees):
        raise ValueError("fleet collector returned an incomplete sessions document")
    by_repo: dict[tuple[str, str], list[dict]] = {}
    for tree in trees:
        if tree["state"] in ("abandoned", "unknown"):
            by_repo.setdefault((tree["state"], tree["repo"]), []).append(tree)
    rows = []
    for (state, repo), found in sorted(by_repo.items()):
        path = str(Path(root) / repo)
        count = len(found)
        listed = [f"{tree.get('path') or '(unnamed)'} ({tree.get('branch') or '?'})" for tree in found]
        if state == "abandoned":
            rows.append({
                "id": f"gone:{repo}", "state": "caution", "type": "worktree registrations", "repo_path": path,
                "what": f"{repo}: {count} {_plural(count, 'worktree', 'worktrees')} registered, directory gone",
                "detail": " · ".join(tree.get("branch") or "?" for tree in found),
                "kind": "Worktrees · directory gone", "facts": {"Repo": path, "Registered": str(count)},
                "list": listed,
            })
        else:
            rows.append({
                "id": f"unread:{repo}", "state": "unknown", "type": "unread registrations", "repo_path": path,
                "what": f"{repo}: {count} {_plural(count, 'registration', 'registrations')} could not be read",
                "detail": "gitdir or HEAD under .git/worktrees was unreadable, so nobody can say whether the directory is there",
                "kind": "Worktrees · unreadable", "facts": {"Repo": path, "Unreadable": str(count)},
                "list": listed,
            })
    if not rows:
        repos = len({tree["repo"] for tree in trees})
        rows.append({
            "id": "wt:ok", "state": "ok", "type": "check",
            "what": "No registration without a directory",
            "detail": f"checked {len(trees)} {_plural(len(trees), 'registration', 'registrations')} "
                      f"in {repos} {_plural(repos, 'checkout', 'checkouts')} under {root}",
            "kind": "Worktrees", "facts": {"Registrations": str(len(trees)), "Root": root},
            "cli": "git worktree list --porcelain  # per repo",
        })
    return rows


def _attribution_rows(count: int) -> list[dict]:
    if count:
        return [{
            "id": "attr:week", "state": "caution", "type": "attribution gap",
            "what": f"{count} {_plural(count, 'commit', 'commits')} of the last 7 days lack Authored-with",
            "detail": "every registered repo, every author, merges left out",
            "kind": "Attribution · last 7 days", "facts": {"Missing": str(count), "Window": "7 days to the reading"},
        }]
    return [{
        "id": "attr:ok", "state": "ok", "type": "check",
        "what": "Every commit of the last 7 days carries Authored-with",
        "detail": "every registered repo, every author, merges left out",
        "kind": "Attribution · last 7 days", "facts": {"Missing": "0", "Window": "7 days to the reading"},
        "cli": "git log --since='7 days ago' --no-merges --format='%(trailers:key=Authored-with,valueonly)'  # per repo",
    }]


def document(connection: sqlite3.Connection, *, now: str, fleet=None, trailers=None) -> dict:
    """Every area of the design, each with its rows, the reason it was not read, and what no reader covers.

    `fleet` is `fleet.collect`'s shape, `area -> document`, and `trailers`
    `reads.missing_trailers`'s; both are the seams a test fills. Each reader
    is guarded on its own, so one failure is its area's `error` and the
    other area still answers.
    """
    read_fleet = fleet or fleet_module.collect
    count_trailers = trailers or reads.missing_trailers
    readers = {
        "attr": lambda: _attribution_rows(count_trailers(connection, now=now)),
        "wt": lambda: _worktree_rows(read_fleet("sessions")),
    }
    areas = []
    for key, name, source, missing in AREAS:
        area = {"id": key, "name": name, "source": source, "read": key in readers, "error": "", "rows": [],
                "missing": list(missing)}
        if key in readers:
            try:
                area["rows"] = readers[key]()
            except (OSError, ValueError, TypeError, KeyError, SdDbError, sqlite3.Error) as failure:
                area["error"] = str(failure) or f"the {name} reader failed without a reason"
        areas.append(area)
    return {"read": now, "areas": areas}
