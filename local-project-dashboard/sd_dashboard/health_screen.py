"""Health: the rows behind the default UI's Health page (sd:2115).

The page is `v2/health.html`; it holds no rows. It reads one JSON document,
`/api/health`, built here from the readers the dashboard already has, so the
port adds no collector:

- Worktrees: `fleet.collect("sessions")`, the child Operations > Sessions and
  Today's Now read. A registration whose directory is gone is a row per
  checkout, carrying the registrations behind it; one whose files could not
  be read is a row of its own, in the unknown state.
- Attribution: `reads.trailer_scan`, the scope the design decided on
  2026-09-30: your commits (each repository's `user.email`) of the last five
  weeks on each default branch (`origin/HEAD`), merges left out, with no
  `Authored-with:` trailer. A repository with no `origin/HEAD`, or no
  `user.email`, is a row that says so. The walk runs inside
  `TRAILER_SECONDS`; past it the area is the refusal and no count.
- Ports: `ports_screen.port_rows` over `collect_ports`, Operations > Ports'
  reader, with its counts and warnings; collected for the request.
- Protection: `protection.rows`, what the nightly `sd shadow sync` left in
  `repo_protection`, with `protection_screen`'s rule: a repository whose
  protection was not read is unknown and shows no cell.
- Disk and Branches: `health_collectors.disk_scan` and `branch_scan`
  (sd:2202, sd:2204), each inside its own budget; that module says what
  each reads and leaves out.

The design source shows nine areas. Three -- Credentials, Dependencies,
Security -- and the parts of the others no reader covers are in the
document as `missing`, by name, so
the page says what it does not read instead of showing a clean lamp. A
reader that fails is its area's `error`, never an empty area: a fleet
nobody could read must not look like a fleet with nothing wrong.

Nothing here writes. The page's fixes are CLI lines for Copy; no route
prunes a worktree or attributes a commit.
"""

from __future__ import annotations

import shlex
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from sd_db import reads, repos
from sd_db.errors import SdDbError

from sd_db import protection as protection_module

from . import fleet as fleet_module
from . import health_collectors, ports_screen
from .operations_screen import TRAILER_SECONDS
from .protection_screen import APPLICABLE, FLAGS, GAPS, ORDER

__all__ = ["AREAS", "document"]

#: The design's areas, in its order: id, name, the reader's source line, and what no reader covers yet.
AREAS = (
    ("disk", "Disk", "df -kPl · du -k -d 1 per disk.conf storage folder · git worktree list per registered repo",
     ("build output sizes (its presence is read, not its size)", "build output in worktrees not yet merged")),
    ("cred", "Credentials", None,
     ("GitHub PAT presence and expiry", "gh CLI sign-in", "HA_TOKEN test", "MCP server status (claude mcp list)")),
    ("attr", "Attribution", "git log origin/HEAD --no-merges --since='5 weeks ago' --author='<user.email>' -i -F per registered repo · %(trailers)",
     ("counts per repo", "the per-week history")),
    ("wt", "Worktrees", "the fleet's .git/worktrees registrations",
     ("merged worktrees still on disk (merge-base --is-ancestor)",)),
    ("br", "Branches", "git for-each-ref --merged=origin/HEAD refs/heads per registered repo", ()),
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
    # A missing root comes back as no worktrees at all, which must not read as "checked 0 registrations".
    if document.get("rootExists") is not True:
        raise ValueError(f"the checkout root {root} does not exist; REPO_ROOT names it")
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


def _attribution_rows(scan: dict) -> list[dict]:
    """The count as one caution or ok row, and one unknown row per reason a repository was not read."""
    missing, commits, repos = scan["missing"], scan["commits"], scan["repos"]
    scope = f"your commits on the default branch of {repos} {_plural(repos, 'repo', 'repos')} · 5 weeks · merges left out"
    facts = {"Missing": str(missing), "Commits": str(commits), "Window": "5 weeks to the reading", "Branch": "origin/HEAD",
             "Author": "each repo's git config user.email"}
    cli = "git -C <repo> log origin/HEAD --no-merges -z --since='5 weeks ago' --author=\"<$(git -C <repo> config user.email)>\" -i -F --format='%H%x1f%(trailers)'"
    if missing:
        rows = [{"id": "attr:weeks", "state": "caution", "type": "attribution gap",
                 "what": f"{missing} of {commits} of your commits in 5 weeks lack Authored-with",
                 "detail": scope, "kind": "Attribution · 5 weeks", "facts": facts}]
    else:
        rows = [{"id": "attr:ok", "state": "ok", "type": "check",
                 "what": f"Each of your {commits} {_plural(commits, 'commit', 'commits')} in 5 weeks carries Authored-with",
                 "detail": scope, "kind": "Attribution · 5 weeks", "facts": facts, "cli": cli}]
    for key, reason, fix in (
            ("no_default", "no origin/HEAD, so the default branch is unknown and nothing was read",
             "git -C <repo> remote set-head origin --auto"),
            ("no_author", "no git config user.email, so there is no author to count",
             "git -C <repo> config user.email <address>")):
        found = scan.get(key) or []
        if found:
            count = len(found)
            rows.append({"id": f"attr:{key}", "state": "unknown", "type": "check",
                         "what": f"{count} {_plural(count, 'repo has', 'repos have')} {reason.split(',')[0]}: not counted",
                         "detail": reason, "kind": "Attribution · not read", "facts": {"Repos": str(count)},
                         "list": list(found), "cli": fix})
    return rows


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


def _size(kb: int) -> str:
    """The design's size wording: MiB, GiB or TiB from a count of KiB."""
    if kb >= 1024 ** 3:
        return f"{kb / 1024 ** 3:.2f} TiB"
    return f"{kb / 1024 ** 2:.1f} GiB" if kb >= 1024 ** 2 else f"{round(kb / 1024)} MiB"


def _unread_repos(found: list[str], area: str, reason: str, fix: str, key: str) -> dict:
    count = len(found)
    return {"id": f"{area}:{key}", "state": "unknown", "type": "check",
            "what": f"{count} {_plural(count, 'repo has', 'repos have')} {reason.split(',')[0]}: not read",
            "detail": reason, "kind": f"{'Branches' if area == 'br' else 'Disk'} · not read",
            "facts": {"Repos": str(count)}, "list": list(found), "cli": fix}


def _branch_rows(scan: dict) -> list[dict]:
    """Per repository, its merged branches `git branch -d` can delete, and those a worktree has checked out."""
    rows = []
    for repo in scan["merged"]:
        name, path = Path(repo["repo"]).name, repo["path"]
        if repo["deletable"]:
            found = repo["deletable"]
            names = [branch for branch, _ in found]
            dates = sorted(date for _, date in found if date)
            rows.append({
                "id": f"br:{repo['repo']}", "state": "caution", "type": "merged branches", "repo_path": path,
                "what": f"{name}: {len(found)} {_plural(len(found), 'merged branch', 'merged branches')} not deleted",
                "detail": " · ".join(names[:4]) + (f" · +{len(names) - 4}" if len(names) > 4 else ""),
                "kind": "Branches · merged",
                "facts": {"Repo": path, "Merged": str(len(found)), "Oldest": dates[0] if dates else "—",
                          "Newest": dates[-1] if dates else "—"},
                "list": [f"{branch} · {date or '?'}" for branch, date in found],
                "cli": f"git -C {shlex.quote(path)} branch -d {' '.join(shlex.quote(branch) for branch in names)}",
            })
        if repo["checked_out"]:
            found = repo["checked_out"]
            rows.append({
                "id": f"brs:{repo['repo']}", "state": "queued", "type": "merged branches", "repo_path": path,
                "what": f"{name}: {len(found)} merged {_plural(len(found), 'branch is', 'branches are')} checked out in a worktree",
                "detail": " · ".join(branch for branch, _ in found[:4]) + (f" · +{len(found) - 4}" if len(found) > 4 else ""),
                "kind": "Branches · checked out", "facts": {"Repo": path, "Count": str(len(found))},
                "list": [f"{branch} · {tree}" for branch, tree in found],
                "disabled": "checked out in a worktree: git branch -d refuses it until the worktree is removed",
                "cli": f"git -C {shlex.quote(path)} worktree list",
            })
    if not rows and scan["repos"]:
        repos = scan["repos"]
        rows.append({"id": "br:ok", "state": "ok", "type": "check", "what": "No merged branch left undeleted",
                     "detail": f"checked the local branches of {repos} {_plural(repos, 'repo', 'repos')} against origin/HEAD",
                     "kind": "Branches", "facts": {"Repos": str(repos)},
                     "cli": "git -C <repo> branch --merged origin/HEAD  # per registered repo"})
    if scan["no_default"]:
        rows.append(_unread_repos(scan["no_default"], "br", "no origin/HEAD, so nothing says which branch is the default",
                                  "git -C <repo> remote set-head origin --auto", "no_default"))
    if scan["unread"]:
        rows.append(_unread_repos(scan["unread"], "br", "a git error, so its branches were not listed",
                                  "git -C <repo> for-each-ref --merged=refs/remotes/origin/HEAD refs/heads/", "unread"))
    if not rows:
        # An empty registry is not a fleet with nothing to delete.
        rows.append({"id": "br:none", "state": "unknown", "type": "check",
                     "what": "No repository registered: Branches has nothing to read",
                     "detail": "register one with sd-db.sh repo add PATH", "kind": "Branches",
                     "facts": {"Registered": "0"}, "cli": "sd-db.sh repo add PATH"})
    return rows


#: The design's volume thresholds, in percent used.
VOLUME_WARNING, VOLUME_CAUTION = 90, 80


def _volume_name(mount: str) -> str:
    return "Mac data" if mount in ("/System/Volumes/Data", "/") else mount.removeprefix("/Volumes/")


def _disk_rows(scan: dict) -> tuple[list[dict], dict]:
    """A row per volume past 80%, the three biggest folders per storage folder, and merged worktrees with build output."""
    rows = []
    volumes = [{**volume, "name": _volume_name(volume["mount"])} for volume in scan["volumes"]]
    for volume in volumes:
        capacity = volume["capacity"]
        if capacity >= VOLUME_CAUTION:
            rows.append({
                "id": f"vol:{volume['mount']}", "state": "warning" if capacity >= VOLUME_WARNING else "caution",
                "type": "volume", "what": f"{volume['name']} is {capacity}% full",
                "detail": f"{volume['mount']} · {_size(volume['avail_kb'])} free of {_size(volume['size_kb'])}",
                "kind": "Disk · volume",
                "facts": {"Mount": volume["mount"], "Used": f"{capacity}%", "Free": _size(volume["avail_kb"]),
                          "Size": _size(volume["size_kb"]), "File system": volume["filesystem"]},
                "cli": f"df -h {shlex.quote(volume['mount'])}",
                "note": f"Health lights caution at {VOLUME_CAUTION}% and warning at {VOLUME_WARNING}%.",
            })
    for entry in scan["storage"]:
        root = entry["root"]
        if entry["error"]:
            rows.append({"id": f"rs:{root}", "state": "unknown", "type": "check", "what": f"{root}: sizes not read",
                         "detail": entry["error"], "kind": "Disk · storage folder", "facts": {"Path": root},
                         "cli": f"du -sh {shlex.quote(root)}/* | sort -h | tail -5"})
            continue
        for folder in entry["folders"][:3]:
            path = folder["path"]
            rows.append({
                "id": f"rs:{path}", "state": "queued", "type": "storage folder",
                "what": f"{Path(root).name}/{Path(path).name} holds {_size(folder['kb'])}",
                "detail": f"{path} · largest of {len(entry['folders'])} {_plural(len(entry['folders']), 'folder', 'folders')} in {root}",
                "kind": "Disk · storage folder", "facts": {"Path": path, "Size": _size(folder["kb"]), "Storage folder": root},
                "cli": f"du -sh {shlex.quote(path)}/* | sort -h | tail -5",
                "note": "Not a fault: a storage folder is where large uncommitted data belongs. Review it when its volume passes 80%.",
            })
    if not scan["storage"]:
        rows.append({"id": "rs:none", "state": "unknown", "type": "check", "what": "No storage folder configured: sizes not read",
                     "detail": f"name each with a storage|<path> line in {scan['config']}"
                               + (f" ({len(scan['refused'])} {_plural(len(scan['refused']), 'line', 'lines')} not understood)" if scan["refused"] else ""),
                     "kind": "Disk · storage folder", "facts": {"Config": scan["config"]}, "list": list(scan["refused"]),
                     "cli": "cp local-project-dashboard/disk.conf.example " + shlex.quote(scan["config"])})
    elif scan["refused"]:
        rows.append({"id": "rs:refused", "state": "unknown", "type": "check",
                     "what": f"{len(scan['refused'])} disk.conf {_plural(len(scan['refused']), 'line', 'lines')} not understood",
                     "detail": f"{scan['config']} · each line is storage|<path>", "kind": "Disk · storage folder",
                     "facts": {"Config": scan["config"]}, "list": list(scan["refused"])})
    build = scan["build"]
    if build["merged"]:
        merged = build["merged"]
        rows.append({
            "id": "build:merged", "state": "caution", "type": "build output",
            "what": f"{len(merged)} merged {_plural(len(merged), 'worktree keeps', 'worktrees keep')} build output",
            "detail": " · ".join(tree["path"] for tree in merged), "kind": "Disk · worktree build output",
            "facts": {"Rule": "delete build output once the PR merges (2026-09-25)", "Checked": f"{build['checked']} worktrees",
                      "Found": str(len(merged))},
            "list": [f"{tree['path']} · {', '.join(name + '/' for name in tree['dirs'])} · {tree['branch'] or 'detached'}" for tree in merged],
            "cli": "\n".join(f"rm -rf {shlex.quote(tree['path'] + '/' + name)}" for tree in merged for name in tree["dirs"]),
        })
    else:
        rows.append({
            "id": "build:merged", "state": "ok", "type": "check", "what": "No merged worktree keeps build output",
            "detail": f"checked {', '.join(name + '/' for name in health_collectors.BUILD_DIRS)} in {build['checked']} registered "
                      f"{_plural(build['checked'], 'worktree', 'worktrees')}",
            "kind": "Disk · worktree build output",
            "facts": {"Rule": "delete build output once the PR merges (2026-09-25)", "Checked": f"{build['checked']} worktrees",
                      "Found": "0 merged with build output"},
            "cli": "git worktree list --porcelain  # per repo, then ls -d <path>/{target,node_modules,.venv}",
        })
    if build["unread"]:
        rows.append({"id": "build:unread", "state": "unknown", "type": "check",
                     "what": f"{len(build['unread'])} {_plural(len(build['unread']), 'worktree', 'worktrees')} with build output: merge not read",
                     "detail": "git merge-base --is-ancestor failed, so nobody can say whether the branch merged",
                     "kind": "Disk · worktree build output", "facts": {"Worktrees": str(len(build["unread"]))},
                     "list": list(build["unread"])})
    return rows, {"volumes": volumes}


#: The whole document's budget. The readers run at once, each inside its own budget, the fleet's 12 seconds the
#: longest, so this is that figure and a margin; a reader still running at it is its area's error.
PAGE_SECONDS = 13.0
#: The areas whose reader walks subprocesses, run in the pool; Protection reads only the database, on this thread.
POOLED = ("disk", "attr", "wt", "br", "ports")


def _settle(area: dict, name: str, read) -> None:
    """One reader's answer into its area: rows and extra, or the reason it has none."""
    try:
        found = read()
        area["rows"], area["extra"] = found if isinstance(found, tuple) else (found, {})
        if "at" in area["extra"]:
            area["at"] = area["extra"].pop("at")
    except reads.OverBudget as refused:
        area["error"] = f"{refused} and was stopped rather than waited on"
    except ports_screen.OverBudget as refused:
        area["error"] = f"the port inventory exceeded its collection budget and was stopped rather than waited on: {refused}"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, SdDbError, sqlite3.Error) as failure:
        area["error"] = str(failure) or f"the {name} reader failed without a reason"


def document(connection: sqlite3.Connection, *, now: str, fleet=None, trailers=None, ports=None, protection=None,
             disk=None, branches=None) -> dict:
    """Every area of the design, each with its rows, the reason it was not read, and what no reader covers.

    `fleet` is `fleet.collect`'s shape, `area -> document`, `trailers`
    `reads.trailer_scan`'s, `ports` `collect_ports`' (no argument) and
    `protection` `protection.rows`', and `disk` and `branches`
    `health_collectors.disk_scan`'s and `branch_scan`'s; each is a seam a test fills. Each reader
    is guarded on its own, so one failure is its area's `error` and the
    others still answer. A reader returns its rows, or its rows and what the
    page draws above them (`extra`).

    **The walkers run at once.** Each waits on subprocesses inside its own
    budget, so in series the budgets add up (the trailer count's 10 seconds,
    Disk's and Branches' 8, the fleet's 12) and at once the slowest decides.
    A sqlite connection belongs to the thread that opened it, so the
    registry the default walkers need is read here first and handed to them
    as paths; Protection, which reads only the database, stays on this
    thread. `PAGE_SECONDS` bounds the whole document: a reader still running
    at it is its area's error, left to stop at its own budget, not waited on.
    """
    try:
        known: list[str] | Exception = [row["path"] for row in repos.registered(connection)]
    except (SdDbError, sqlite3.Error) as failure:
        known = failure

    def registered() -> list[str]:
        if isinstance(known, Exception):
            raise known
        return known

    read_fleet = fleet or fleet_module.collect
    count_trailers = trailers or (lambda connection, *, now: reads.trailer_scan(
        connection, now=now, within=TRAILER_SECONDS, repo_paths=registered()))
    collect_ports = ports or ports_screen._collect
    read_protection = protection or protection_module.rows
    scan_disk = disk or (lambda connection: health_collectors.disk_scan(connection, repo_paths=registered()))
    scan_branches = branches or (lambda connection: health_collectors.branch_scan(connection, repo_paths=registered()))
    readers = {
        "disk": lambda: _disk_rows(scan_disk(connection)),
        "attr": lambda: _attribution_rows(count_trailers(connection, now=now)),
        "wt": lambda: _worktree_rows(read_fleet("sessions")),
        "br": lambda: _branch_rows(scan_branches(connection)),
        "ports": lambda: _port_rows(collect_ports()),
        "prot": lambda: _protection_rows(read_protection(connection)),
    }
    pool = ThreadPoolExecutor(max_workers=len(POOLED), thread_name_prefix="health")
    try:
        running = {key: pool.submit(readers[key]) for key in POOLED}
        stop = time.monotonic() + PAGE_SECONDS
        areas = []
        for key, name, source, missing in AREAS:
            area = {"id": key, "name": name, "source": source, "read": key in readers, "error": "", "rows": [],
                    "missing": list(missing), "at": now if key in readers else None, "extra": {}}
            if key in running:
                try:
                    running[key].result(timeout=max(0.0, stop - time.monotonic()))
                except FutureTimeout:
                    area["error"] = (f"the {name} reader was still running at the page's budget of {PAGE_SECONDS:g} "
                                     "seconds and was left rather than waited on")
                except BaseException:  # noqa: BLE001 - the reader's own error is settled below
                    pass
                if not area["error"]:
                    _settle(area, name, running[key].result)
            elif key in readers:
                _settle(area, name, readers[key])
            areas.append(area)
    finally:
        # A reader past the page's budget keeps its thread until its own budget stops it; nobody waits for it.
        pool.shutdown(wait=False, cancel_futures=True)
    return {"read": now, "areas": areas}
