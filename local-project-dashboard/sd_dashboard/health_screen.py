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
- Ports: `ports_screen.port_rows` over `collect_ports`, Operations > Ports'
  reader, with its counts and warnings; collected for the request.
- Protection: `protection.rows`, what the nightly `sd shadow sync` left in
  `repo_protection`, with `protection_screen`'s rule: a repository whose
  protection was not read is unknown and shows no cell.

The design source shows nine areas. Five -- Disk, Credentials,
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

from sd_db import protection as protection_module

from . import fleet as fleet_module
from . import ports_screen
from .protection_screen import APPLICABLE, FLAGS, GAPS, ORDER

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
    ("ports", "Ports", "collect_ports · lsof -nP -iTCP -sTCP:LISTEN · docker ps", ()),
    ("prot", "Protection", "sd shadow sync → repo_protection", ()),
)
#: The matrix lines, in the design's order: v1's gap columns, then its merge-setting flags.
CHECKS = GAPS + FLAGS
LISTENER = {"listening": "Listening", "not_listening": "No listener observed",
            "unknown": "Unknown", "unconfigured": "No port configured"}


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


def _port_rows(snapshot) -> tuple[list[dict], dict]:
    """Operations > Ports' rows as ledger rows, with its counts line and warnings."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("services"), list):
        raise ValueError("invalid port collector output")
    found = ports_screen.port_rows(snapshot)
    if not found and not snapshot.get("complete"):
        raise ValueError("port inventory is unavailable")
    unique = {row["port"]: row for row in found if row["port"]}
    counts = {"configured": sum(row["configured"] for row in unique.values()),
              "listening": sum(row["listener"] == "listening" for row in unique.values()),
              "unknown": sum(row["listener"] == "unknown" for row in unique.values())}
    observed = snapshot.get("observed")
    warnings = [text for flagged, text in (
        (not snapshot.get("complete"), "Inventory collection was incomplete; listed observations may be partial."),
        (counts["unknown"], f"Listener state is unknown for {counts['unknown']} configured ports."),
        (not (isinstance(observed, dict) and observed.get("complete")),
         "The wider TCP listener inventory is incomplete or unavailable; positive observations remain visible."),
    ) if flagged]
    rows = []
    for port in found:
        listener = LISTENER[port["listener"]]
        state = ("caution" if port["clash"] or port["busy"] else
                 ("ok" if port["configured"] else "queued") if port["listener"] == "listening" else
                 "unknown" if port["listener"] == "unknown" else "queued")
        holder = port["process"] or ("Listener inspection unavailable" if port["listener"] == "unknown" else listener)
        configured = port["configured"]
        rows.append({
            "id": f"port:{port['service']}:{port['port'] or 'none'}" if configured else f"port:observed:{port['port']}",
            "state": state, "type": "port", "port": port["port"],
            "what": f"{port['service']} {':' + port['port'] if port['port'] else '· no port configured'}" if configured
                    else f":{port['port']} observed only",
            "detail": f"{listener} · {holder} · container {port['container_display'].lower()} · {port['profile']}",
            "kind": "Port · configured service" if configured else "Port · observed only",
            "facts": {"Service": port["service"], "Port": port["port"] or "—", "Listener": listener, "Process": holder,
                      "Container": port["container_display"], "Profile": port["profile"]},
            "note": "Unknown: the listener was not inspected, so nothing says whether the port is open or free."
                    if port["listener"] == "unknown" else
                    "No configured service claims this port; the row shows what the Mac reported and nothing else."
                    if not configured else "",
        })
    return rows, {"counts": counts, "warnings": warnings}


def _cell(row: dict, check: str) -> list[str]:
    """One matrix cell: ['gap', sentence], ['ok'], ['na'] (does not apply) or ['unknown'] (not read)."""
    if row["status"] == "unknown":
        return ["unknown"]
    if check in dict(FLAGS):
        flag = next((flag for flag in row["merge_settings"] if flag.get("id") == check), None)
        if flag is None:
            return ["na"]
        return ["gap", f"{flag.get('value') or ''} · {flag.get('gap') or ''}"] if flag.get("flagged") else ["ok"]
    if check not in APPLICABLE.get(row["status"], frozenset()):
        return ["na"]
    gap = next((gap for gap in row["gaps"] if gap.get("id") == check), None)
    return ["gap", gap.get("gap") or ""] if gap else ["ok"]


def _protection_rows(found: list[dict]) -> tuple[list[dict], dict]:
    """One ledger row per registered repository, and the matrix's column order: unprotected, unknown, protected."""
    rows = []
    for repo in found:
        status = repo["status"] if repo["status"] in ORDER else "unknown"
        repo = {**repo, "status": status}
        cells = [[label, *_cell(repo, check)] for check, label in CHECKS]
        gaps = [cell for cell in cells if cell[1] == "gap"]
        name = repo.get("slug") or repo["repo"]
        branch = repo.get("default_branch") or "—"
        unprotected = next((gap.get("gap") for gap in repo["gaps"] if gap.get("id") == "unprotected"), None)
        rows.append({
            "id": f"prot:{repo['repo']}", "type": "branch protection", "slug": repo.get("slug"),
            "state": "warning" if status == "unprotected" else "unknown" if status == "unknown" else "caution" if gaps else "ok",
            "what": f"{name}: protection not read" if status == "unknown" else
                    f"{name}: {branch} is unprotected" if status == "unprotected" else
                    f"{name}: {len(gaps)} {_plural(len(gaps), 'gap', 'gaps')}" if gaps else f"{name}: protected, no gap",
            "detail": repo.get("reason") or "unknown" if status == "unknown" else
                      f"{branch} · {', '.join(cell[0] for cell in gaps) if gaps else 'every check passes'}",
            "kind": "Branch protection · " + status,
            "facts": {"Repository": repo["repo"], "Slug": repo.get("slug") or "no github.com remote", "Status": status,
                      "Branch": branch, "Observed": repo.get("observed_at") or "never"},
            "status": status, "name": name, "branch": branch, "reason": repo.get("reason") or "unknown" if status == "unknown" else "",
            "sentence": unprotected or "", "cells": [] if status == "unknown" else cells, "gaps": len(gaps),
        })
    if not rows:
        # An empty registry is not a protected fleet.
        rows.append({"id": "prot:none", "state": "unknown", "type": "check",
                     "what": "No repository registered: protection has nothing to read",
                     "detail": "register one with sd-db.sh repo add PATH, then run sd shadow sync",
                     "kind": "Branch protection", "facts": {"Registered": "0"},
                     "cli": "sd-db.sh repo add PATH && sd shadow sync"})
        return rows, {"counts": dict.fromkeys(ORDER, 0), "checks": [label for _, label in CHECKS], "columns": [],
                      "reasons": [], "at": None}
    order = sorted(rows, key=lambda row: (ORDER[row["status"]], -row["gaps"], row["name"]))
    counts = {status: sum(row["status"] == status for row in rows) for status in ORDER}
    observed = [repo["observed_at"] for repo in found if repo.get("observed_at")]
    return rows, {"counts": counts, "checks": [label for _, label in CHECKS], "columns": [row["id"] for row in order],
                  "reasons": sorted({row["reason"] for row in rows if row["status"] == "unknown"}),
                  "at": max(observed) if observed else None}


def document(connection: sqlite3.Connection, *, now: str, fleet=None, trailers=None, ports=None, protection=None) -> dict:
    """Every area of the design, each with its rows, the reason it was not read, and what no reader covers.

    `fleet` is `fleet.collect`'s shape, `area -> document`, `trailers`
    `reads.missing_trailers`'s, `ports` `collect_ports`' (no argument) and
    `protection` `protection.rows`'; each is a seam a test fills. Each reader
    is guarded on its own, so one failure is its area's `error` and the
    others still answer. A reader returns its rows, or its rows and what the
    page draws above them (`extra`).
    """
    read_fleet = fleet or fleet_module.collect
    count_trailers = trailers or reads.missing_trailers
    collect_ports = ports or ports_screen._collect
    read_protection = protection or protection_module.rows
    readers = {
        "attr": lambda: _attribution_rows(count_trailers(connection, now=now)),
        "wt": lambda: _worktree_rows(read_fleet("sessions")),
        "ports": lambda: _port_rows(collect_ports()),
        "prot": lambda: _protection_rows(read_protection(connection)),
    }
    areas = []
    for key, name, source, missing in AREAS:
        area = {"id": key, "name": name, "source": source, "read": key in readers, "error": "", "rows": [],
                "missing": list(missing), "at": now if key in readers else None, "extra": {}}
        if key in readers:
            try:
                found = readers[key]()
                area["rows"], area["extra"] = found if isinstance(found, tuple) else (found, {})
                if "at" in area["extra"]:
                    area["at"] = area["extra"].pop("at")
            except ports_screen.OverBudget as refused:
                area["error"] = f"the port inventory exceeded its collection budget and was stopped rather than waited on: {refused}"
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, SdDbError, sqlite3.Error) as failure:
                area["error"] = str(failure) or f"the {name} reader failed without a reason"
        areas.append(area)
    return {"read": now, "areas": areas}
