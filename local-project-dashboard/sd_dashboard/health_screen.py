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
- Dependencies and Security (sd:2205, sd:2206): the same `protection.rows`,
  each managed repository's `alerts`, which the nightly sync stores beside
  its protection. Health makes no GitHub call.
- Credentials (sd:2203): the latest `credentials:nightly` heartbeat that
  `sd-db.sh credentials` writes each night: presence, validity and expiry,
  never a value. Expiry is judged against the request's time.

The design source shows nine areas. The parts no reader covers are in the
document as `missing`, by name, so
the page says what it does not read instead of showing a clean lamp. A
reader that fails is its area's `error`, never an empty area: a fleet
nobody could read must not look like a fleet with nothing wrong.

Nothing here writes. The page's fixes are CLI lines for Copy; no route
prunes a worktree or attributes a commit.
"""

from __future__ import annotations

import importlib
import shlex
import sqlite3
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import Future
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
    ("cred", "Credentials", "sd-db.sh credentials (nightly) → state heartbeat credentials:nightly", ()),
    ("attr", "Attribution", "git log origin/HEAD --no-merges --since='5 weeks ago' --author='<user.email>' -i -F per registered repo · %(trailers)",
     ("counts per repo", "the per-week history")),
    ("wt", "Worktrees", "the fleet's .git/worktrees registrations",
     ("merged worktrees still on disk (merge-base --is-ancestor)",)),
    ("br", "Branches", "git for-each-ref --merged=origin/HEAD refs/heads per registered repo", ()),
    ("dep", "Dependencies", "sd shadow sync → repo_protection alerts · dependabot/alerts?state=open per managed repo", ()),
    ("sec", "Security", "sd shadow sync → repo_protection alerts · security_and_analysis · secret-scanning/alerts per public managed repo", ()),
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
                      "Branch": branch, "Observed": repo.get("observed_at") or "never",
                      # A sibling checkout's row, read for this one (sd:1607).
                      **({"Borrowed from": repo["borrowed_from"]} if repo.get("borrowed_from") else {})},
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


def _managed(found: list[dict]) -> list[dict]:
    """The managed repositories, one per GitHub repository: of sibling checkouts, the newest observation."""
    if found and not any("managed" in repo for repo in found):
        # An installed sd_db older than sd:2205 reports neither flag nor alerts: not "no managed repository".
        raise ValueError("the installed sd_db reports no managed flag; install the build that stores alerts (sd:2205)")
    newest: dict[str, dict] = {}
    for repo in found:
        key = repo.get("slug") or repo["repo"]
        if repo.get("managed") and (key not in newest or (repo.get("observed_at") or "") > (newest[key].get("observed_at") or "")):
            newest[key] = repo
    return list(newest.values())


def _alert_extra(managed: list[dict]) -> dict:
    observed = [repo["observed_at"] for repo in managed if repo.get("observed_at")]
    return {"at": max(observed) if observed else None}


def _no_managed(area: str, name: str) -> dict:
    # An empty list is not a clean fleet.
    return {"id": f"{area}:none", "state": "unknown", "type": "check",
            "what": f"No managed repository: {name} has nothing to read",
            "detail": "the nightly sync reads alerts for managed repositories only",
            "kind": name, "facts": {"Managed": "0"}, "cli": "sd-db.sh repo managed PATH yes && sd shadow sync"}


def _not_read(area: str, name: str, unread: list[str]) -> dict:
    count = len(unread)
    return {"id": f"{area}:unread", "state": "unknown", "type": "check",
            "what": f"{count} managed {_plural(count, 'repo', 'repos')}: {name.lower()} not read",
            "detail": "a read that failed is not a count of zero", "kind": f"{name} · not read",
            "facts": {"Repos": str(count)}, "list": unread, "cli": "sd shadow sync"}


def _unread_reason(repo: dict, part: dict | None) -> str:
    name = repo.get("slug") or repo["repo"]
    if repo.get("alerts") is None:
        return f"{name}: {repo.get('reason') or 'not read yet; the next sd shadow sync reads it'}"
    return f"{name}: {(part or {}).get('reason') or 'not read'}"


def _dependency_rows(found: list[dict]) -> tuple[list[dict], dict]:
    """A row per managed repository with open Dependabot alerts, one for those not read, and the clean count."""
    managed = _managed(found)
    if not managed:
        return [_no_managed("dep", "Dependencies")], {}
    rows, unread, clean, archived = [], [], 0, 0
    for repo in managed:
        part = (repo.get("alerts") or {}).get("dependabot")
        name = repo.get("slug") or repo["repo"]
        if isinstance(part, dict) and part.get("archived"):
            archived += 1
        elif not isinstance(part, dict) or not isinstance(part.get("open"), int):
            unread.append(_unread_reason(repo, part))
        elif not part["open"]:
            clean += 1
        else:
            count = f"{part['open']}{'+' if part.get('more') else ''}"
            severity = part.get("severity") if isinstance(part.get("severity"), dict) else {}
            grave = sum(severity.get(level, 0) for level in ("critical", "high"))
            rows.append({
                "id": f"dep:{repo['repo']}", "state": "warning" if grave else "caution", "type": "dependabot alerts",
                "slug": repo.get("slug"),
                "what": f"{name}: {count} open Dependabot {_plural(part['open'], 'alert', 'alerts')}",
                "detail": " · ".join(f"{severity[level]} {level}" for level in ("critical", "high", "medium", "low", "unknown")
                                     if severity.get(level)) or "severity not read",
                "kind": "Dependencies · open alerts",
                "facts": {"Repository": repo["repo"], "Open": str(part["open"]), "Shown": count,
                          "Observed": repo.get("observed_at") or "never"},
                "cli": f"gh api {shlex.quote(f'repos/{name}/dependabot/alerts?state=open')} --jq '.[].html_url'",
            })
    if unread:
        rows.append(_not_read("dep", "Dependencies", unread))
    if clean and not any(row["type"] == "dependabot alerts" for row in rows):
        rows.append({"id": "dep:ok", "state": "ok", "type": "check",
                     "what": f"No open Dependabot alert in {clean} managed {_plural(clean, 'repo', 'repos')}",
                     "detail": f"archived repos left out: {archived}", "kind": "Dependencies",
                     "facts": {"Clean": str(clean), "Archived": str(archived)}})
    return rows, _alert_extra(managed)


def _security_rows(found: list[dict]) -> tuple[list[dict], dict]:
    """Per public managed repository: open secret-scanning alerts, or scanning off; private ones are not scanned."""
    managed = _managed(found)
    if not managed:
        return [_no_managed("sec", "Security")], {}
    rows, unread, clean, private = [], [], 0, 0
    for repo in managed:
        part = (repo.get("alerts") or {}).get("secret_scanning")
        name = repo.get("slug") or repo["repo"]
        settings = f"https://github.com/{name}/settings/security_analysis"
        if isinstance(part, dict) and part.get("visibility") == "private":
            private += 1
        elif isinstance(part, dict) and part.get("setting") == "disabled":
            rows.append({"id": f"sec:off:{repo['repo']}", "state": "caution", "type": "secret scanning",
                         "slug": repo.get("slug"), "what": f"{name}: public, secret scanning off",
                         "detail": "a public repo with scanning off is a finding", "kind": "Security · scanning off",
                         "facts": {"Repository": repo["repo"], "Setting": "disabled",
                                   "Observed": repo.get("observed_at") or "never"},
                         "cli": f"open {settings}"})
        elif not isinstance(part, dict) or not isinstance(part.get("open"), int):
            unread.append(_unread_reason(repo, part))
        elif not part["open"]:
            clean += 1
        else:
            count = f"{part['open']}{'+' if part.get('more') else ''}"
            rows.append({"id": f"sec:{repo['repo']}", "state": "warning", "type": "secret scanning",
                         "slug": repo.get("slug"),
                         "what": f"{name}: {count} open secret-scanning {_plural(part['open'], 'alert', 'alerts')}",
                         "detail": "rotate the secret first, then close the alert", "kind": "Security · open alerts",
                         "facts": {"Repository": repo["repo"], "Open": str(part["open"]), "Shown": count,
                                   "Observed": repo.get("observed_at") or "never"},
                         "cli": f"gh api {shlex.quote(f'repos/{name}/secret-scanning/alerts?state=open')} --jq '.[].html_url'"})
    if unread:
        rows.append(_not_read("sec", "Security", unread))
    if clean and not any(row["type"] == "secret scanning" for row in rows):
        rows.append({"id": "sec:ok", "state": "ok", "type": "check",
                     "what": f"Scanning on, no open alert, in {clean} public managed {_plural(clean, 'repo', 'repos')}",
                     "detail": f"private repos are not scanned, by policy: {private}", "kind": "Security",
                     "facts": {"Clean": str(clean), "Private": str(private)}})
    return rows, _alert_extra(managed)


#: Days before a credential's expiry that Health lights caution, then warning.
EXPIRY_CAUTION, EXPIRY_WARNING = 30, 7
#: A heartbeat older than this means the nightly job has stopped.
CREDENTIALS_STALE_HOURS = 48
CREDENTIALS_CLI = "local-sd-db/sd-db.sh credentials"


def read_credentials(connection: sqlite3.Connection) -> tuple[str, dict] | None:
    """`sd_db.credentials.read`, imported here so an installed library older than sd:2203 is the area's error."""
    try:
        credentials = importlib.import_module("sd_db.credentials")
    except ImportError as missing:
        raise ValueError("the installed sd_db has no credentials module; install the build with sd:2203") from missing
    return credentials.read(connection)


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)


def _credential_row(probe: dict, now: datetime) -> dict:
    """One probe as a ledger row: absent or rejected is a warning, an expiry near or past lights by `EXPIRY_*`."""
    name, pid = probe.get("name") or probe.get("id") or "?", probe.get("id") or "?"
    row = {"id": f"cred:{pid}", "type": "credential", "kind": "Credentials · " + name.split(" (")[0],
           "facts": {"Credential": name}, "cli": CREDENTIALS_CLI}
    facts = row["facts"]
    for key, label in (("present", "Present"), ("valid", "Accepted"), ("signed_in", "Signed in"), ("expires", "Expires")):
        if key in probe:
            facts[label] = "—" if probe[key] is None else str(probe[key]).lower() if isinstance(probe[key], bool) else probe[key]
    servers = probe.get("servers")
    if isinstance(servers, list):
        down = [server for server in servers if isinstance(server, dict) and server.get("status") != "connected"]
        facts["Servers"] = str(len(servers))
        if down:
            return {**row, "state": "caution", "what": f"{len(down)} of {len(servers)} MCP servers not connected",
                    "detail": " · ".join(f"{s.get('name')} {s.get('status')}" for s in down[:4]),
                    "list": [f"{s.get('name')} · {s.get('status')}" for s in down], "cli": "claude mcp list"}
        return {**row, "state": "ok", "what": f"All {len(servers)} MCP servers connected", "detail": name}
    if probe.get("present") is False:
        return {**row, "state": "warning", "what": f"{name}: absent", "detail": "not set in the job's environment (env.sh)"}
    if probe.get("valid") is False or probe.get("signed_in") is False:
        return {**row, "state": "warning", "what": f"{name}: rejected",
                "detail": "the service refused it" if "valid" in probe else "gh auth status failed"}
    if probe.get("reason"):
        return {**row, "state": "unknown", "what": f"{name}: not read", "detail": probe["reason"]}
    if probe.get("expires"):
        days = (_when(probe["expires"]) - now).days
        state = "warning" if days < EXPIRY_WARNING else "caution" if days < EXPIRY_CAUTION else "ok"
        return {**row, "state": state, "what": f"{name}: expired" if days < 0 else f"{name}: expires in {days} days",
                "detail": f"expires {probe['expires']}",
                "note": f"Health lights caution {EXPIRY_CAUTION} days before expiry and warning at {EXPIRY_WARNING}."}
    if probe.get("valid") or probe.get("signed_in"):
        return {**row, "state": "ok", "what": f"{name}: accepted",
                "detail": "no expiry reported" if "expires" in probe else name}
    return {**row, "state": "unknown", "what": f"{name}: not read", "detail": "the probe recorded no answer"}


def _credential_rows(found: tuple[str, dict] | None, *, now: str) -> tuple[list[dict], dict]:
    """A row per probe of the last nightly run, and a caution row when that run is stale."""
    if found is None:
        return [{"id": "cred:none", "state": "unknown", "type": "check", "what": "Credentials not yet observed",
                 "detail": "the nightly job writes presence and expiry: local-cron-jobs/examples/credentials-nightly.job",
                 "kind": "Credentials", "facts": {"Heartbeat": "credentials:nightly"}, "cli": CREDENTIALS_CLI}], {"at": None}
    stamp, body = found
    moment = _when(now)
    rows = [_credential_row(probe, moment) for probe in body["probes"] if isinstance(probe, dict)]
    age = (moment - _when(stamp)).total_seconds() / 3600
    if age > CREDENTIALS_STALE_HOURS:
        rows.append({"id": "cred:stale", "state": "caution", "type": "check",
                     "what": f"Credentials last observed {round(age / 24)} days ago",
                     "detail": f"the nightly job has not recorded since {stamp}", "kind": "Credentials",
                     "facts": {"Observed": stamp}, "cli": CREDENTIALS_CLI})
    return rows, {"at": stamp}


#: The whole document's budget. The readers run at once, each inside its own budget, the fleet's 12 seconds the
#: longest, so this is that figure and a margin; a reader still running at it is its area's error.
PAGE_SECONDS = 13.0
#: The areas whose reader walks subprocesses, each on its own scan thread; Protection, Dependencies, Security and
#: Credentials read only the database, on this thread.
POOLED = ("disk", "attr", "wt", "br", "ports")


class _Scans:
    """At most one scan per pooled area at a time, and each area's last answer.

    The page made a thread pool per request and left a reader past its budget
    running: Disk's `isdir()` and config reads sit outside any subprocess
    timeout, so a stalled mount held its thread for good, each refresh added
    one, and the interpreter waited for all of them at exit (sd:2520 review).
    Now a request joins an area's scan that is still running instead of
    starting a second, so a blocked reader holds one thread however often the
    page is read. The thread is a daemon, so exit does not wait on it.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running: dict[str, tuple[str, Future]] = {}
        self.last: dict[str, tuple[str, object]] = {}

    def start(self, key: str, read, *, now: str) -> tuple[str, Future, bool]:
        """The area's scan, the time it started and whether it was already running."""
        with self.lock:
            running, last = self.running, self.last
            held = running.get(key)
            if held and not held[1].done():
                return held[0], held[1], True
            future: Future = Future()
            running[key] = (now, future)

        def scan():
            try:
                found = read()
            except BaseException as failure:  # noqa: BLE001 - the request that reads the future settles it
                future.set_exception(failure)
            else:
                with self.lock:
                    last[key] = (now, found)
                future.set_result(found)
        threading.Thread(target=scan, name=f"health-{key}", daemon=True).start()
        return now, future, False

    def clear(self) -> None:
        """Forget every scan and answer; a scan still running writes only into what it started with."""
        with self.lock:
            self.running, self.last = {}, {}


_SCANS = _Scans()


def _settle(area: dict, name: str, read) -> None:
    """One reader's answer into its area: rows and extra, or the reason it has none."""
    try:
        found = read()
        area["rows"], extra = found if isinstance(found, tuple) else (found, {})
        area["extra"] = dict(extra)  # a kept answer is settled again; popping `at` must not change it
        if "at" in area["extra"]:
            area["at"] = area["extra"].pop("at")
    except reads.OverBudget as refused:
        area["error"] = f"{refused} and was stopped rather than waited on"
    except ports_screen.OverBudget as refused:
        area["error"] = f"the port inventory exceeded its collection budget and was stopped rather than waited on: {refused}"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, SdDbError, sqlite3.Error) as failure:
        area["error"] = str(failure) or f"the {name} reader failed without a reason"


def document(connection: sqlite3.Connection, *, now: str, fleet=None, trailers=None, ports=None, protection=None,
             disk=None, branches=None, credentials=None) -> dict:
    """Every area of the design, each with its rows, the reason it was not read, and what no reader covers.

    `fleet` is `fleet.collect`'s shape, `area -> document`, `trailers`
    `reads.trailer_scan`'s, `ports` `collect_ports`' (no argument) and
    `protection` `protection.rows`', and `disk` and `branches`
    `health_collectors.disk_scan`'s and `branch_scan`'s, and `credentials`
    `read_credentials`'; each is a seam a test fills. Each reader
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
    at it is left to stop at its own budget, not waited on. While it runs, a
    later request starts no second scan of that area (`_Scans`); the area
    shows its last answer marked `stale`, or is its error if it has none.
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
    read_creds = credentials or read_credentials
    scan_disk = disk or (lambda connection: health_collectors.disk_scan(connection, repo_paths=registered()))
    scan_branches = branches or (lambda connection: health_collectors.branch_scan(connection, repo_paths=registered()))
    readers = {
        "disk": lambda: _disk_rows(scan_disk(connection)),
        "attr": lambda: _attribution_rows(count_trailers(connection, now=now)),
        "wt": lambda: _worktree_rows(read_fleet("sessions")),
        "br": lambda: _branch_rows(scan_branches(connection)),
        "ports": lambda: _port_rows(collect_ports()),
        "prot": lambda: _protection_rows(read_protection(connection)),
        "dep": lambda: _dependency_rows(read_protection(connection)),
        "sec": lambda: _security_rows(read_protection(connection)),
        "cred": lambda: _credential_rows(read_creds(connection), now=now),
    }
    running = {key: _SCANS.start(key, readers[key], now=now) for key in POOLED}
    stop = time.monotonic() + PAGE_SECONDS
    areas = []
    for key, name, source, missing in AREAS:
        area = {"id": key, "name": name, "source": source, "read": key in readers, "error": "", "stale": "",
                "rows": [], "missing": list(missing), "at": now if key in readers else None, "extra": {}}
        if key in running:
            since, scan, joined = running[key]
            try:
                scan.result(timeout=max(0.0, stop - time.monotonic()))
            except FutureTimeout:
                why = (f"the {name} reader was still running at the page's budget of {PAGE_SECONDS:g} "
                       "seconds and was left rather than waited on")
                if joined:
                    why += f"; that scan started at {since}, and no second one starts while it runs"
                kept = _SCANS.last.get(key)
                if kept:
                    area["stale"], area["at"] = f"{why}; these rows are the read of {kept[0]}", kept[0]
                    _settle(area, name, lambda: kept[1])
                else:
                    area["error"] = why
            except BaseException:  # noqa: BLE001 - the reader's own error is settled below
                pass
            if not area["error"] and not area["stale"]:
                area["at"] = since
                _settle(area, name, scan.result)
        elif key in readers:
            _settle(area, name, readers[key])
        areas.append(area)
    return {"read": now, "areas": areas}
