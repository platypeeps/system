"""The Disk and Branches readers behind Health (sd:2202, sd:2204).

Both read this machine at request time and write nothing, as the other
Health readers do. Each runs inside one overall budget, as the trailer count
does (`reads.trailer_scan`'s `within`): a command that would end past it is
stopped, and the reader raises `reads.OverBudget`, which Health shows as the
area stopped rather than waited on. A partial walk is not the answer.

**Branches** (sd:2204) is the design's "merged but not deleted": the local
branches of each registered repository that `origin/HEAD` already contains,
`git for-each-ref --merged=refs/remotes/origin/HEAD refs/heads/`. The default
branch itself is left out. A merged branch checked out in a worktree is its
own list, because `git branch -d` refuses it. A repository with no
`origin/HEAD`, or one git cannot read, is named rather than read as clean.

**Disk** (sd:2202) is the design's "where the space went", in three parts:

- volume use from `df -kPl`, local file systems only, so a network mount
  that does not answer cannot hold the page: the data volume (`/` where
  there is no `/System/Volumes/Data`) and every volume under `/Volumes`;
- the folder sizes under each `storage|<path>` line of
  `<config>/project-dashboard/disk.conf`, one `du -k -d 1` per folder with
  what is left of the budget; a folder `du` could not finish is a row that
  says so, not a refusal of the area;
- registered worktrees whose `HEAD` is in `origin/HEAD` and that still hold
  `target/`, `node_modules/` or `.venv/` (the 2026-09-25 rule: delete build
  output once the PR merges). Presence only: sizing build output needs a
  `du` of every tree, which the budget does not hold.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from sd_db import paths, reads, repos

__all__ = ["BRANCH_SECONDS", "DISK_SECONDS", "branch_scan", "disk_scan", "storage_roots"]

#: Each reader's overall budget. Branches is two git calls per registered repository, as the trailer count is
#: one; Disk is df, a few git calls per repository and the du walks, which get what is left.
BRANCH_SECONDS = 8.0
DISK_SECONDS = 8.0
#: One command's own ceiling inside the budget, the trailer count's figure.
COMMAND_SECONDS = 20.0
#: The build output folders the 2026-09-25 rule names.
BUILD_DIRS = ("target", "node_modules", ".venv")


class Walk:
    """Commands run inside one overall budget; past it, `reads.OverBudget` names what was stopped."""

    def __init__(self, within: float, noun: str):
        self.stop = time.monotonic() + within
        self.refusal = f"{noun} ran past its budget of {within:g} seconds"

    def left(self) -> float:
        return self.stop - time.monotonic()

    def run(self, argv: list[str], *, spare: bool = False) -> subprocess.CompletedProcess | None:
        """The command's answer, or None when it could not answer.

        `spare` is for a command whose overrun is its own row: its timeout
        returns None and leaves the walk to say what it did not finish.
        """
        timeout = min(COMMAND_SECONDS, self.left())
        if timeout <= 0:
            raise reads.OverBudget(self.refusal)
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            if not spare and self.left() <= 0:
                raise reads.OverBudget(self.refusal) from None
            return None
        except (OSError, subprocess.SubprocessError):
            return None

    def git(self, path: Path, *args: str) -> subprocess.CompletedProcess | None:
        return self.run(["git", "-C", str(path), *args])


def _registered(connection: sqlite3.Connection) -> list[str]:
    return [row["path"] for row in repos.registered(connection)]


def _default(walk: Walk, path: Path) -> tuple[str, str | None]:
    """('ok', branch name) for a repository with origin/HEAD; ('no_default', None) or ('unread', None) otherwise."""
    found = walk.git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if found is None or found.returncode not in (0, 1):
        return "unread", None
    if found.returncode == 1 or not found.stdout.strip():
        return "no_default", None
    return "ok", found.stdout.strip().split("/", 1)[-1]


def branch_scan(connection: sqlite3.Connection, *, within: float = BRANCH_SECONDS) -> dict:
    """Each registered repository's local branches already in `origin/HEAD`, and the repositories not read."""
    walk = Walk(within, "the merged-branch walk")
    scan: dict = {"repos": 0, "merged": [], "no_default": [], "unread": []}
    for key in _registered(connection):
        path = paths.disk(key)
        state, default = _default(walk, path)
        if state != "ok":
            scan[state].append(key)
            continue
        refs = walk.git(path, "for-each-ref", "--merged=refs/remotes/origin/HEAD",
                        "--format=%(refname:short)%09%(committerdate:short)%09%(worktreepath)", "refs/heads/")
        if refs is None or refs.returncode != 0:
            scan["unread"].append(key)
            continue
        scan["repos"] += 1
        deletable, checked_out = [], []
        for line in refs.stdout.splitlines():
            name, _, rest = line.partition("\t")
            date, _, tree = rest.partition("\t")
            if not name or name == default:
                continue
            (checked_out.append((name, tree)) if tree else deletable.append((name, date)))
        if deletable or checked_out:
            scan["merged"].append({"repo": key, "path": str(path), "deletable": deletable, "checked_out": checked_out})
    return scan


def _storage_config() -> Path:
    lib = str(Path(__file__).resolve().parents[2] / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import system_tools_config
    return system_tools_config.config_dir("project-dashboard") / "disk.conf"


def storage_roots(config: Path | None = None) -> tuple[list[Path], list[str], Path]:
    """The `storage|<path>` folders of `disk.conf`, the lines it could not read, and the file read.

    `~` and `$HOME` expand; any other line is named, never guessed at.
    """
    config = config or _storage_config()
    roots, refused = [], []
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return roots, refused, config
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, value = line.partition("|")
        value = value.strip().replace("$HOME", "~", 1) if value.strip().startswith("$HOME") else value.strip()
        if kind.strip() != "storage" or not value:
            refused.append(f"line {number}: {line}")
            continue
        roots.append(Path(value).expanduser())
    return roots, refused, config


def _volumes(walk: Walk) -> list[dict]:
    done = walk.run(["df", "-kPl"])
    if done is None or done.returncode not in (0, 1) or not done.stdout.strip():
        raise ValueError("df -kPl gave no volume table")
    rows = []
    for line in done.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6 or not fields[1].isdigit():
            continue
        mount = " ".join(fields[5:])
        rows.append({"filesystem": fields[0], "size_kb": int(fields[1]), "used_kb": int(fields[2]),
                     "avail_kb": int(fields[3]), "capacity": int(fields[4].rstrip("%") or 0), "mount": mount})
    data = any(row["mount"] == "/System/Volumes/Data" for row in rows)
    kept = [row for row in rows if row["mount"] == ("/System/Volumes/Data" if data else "/")
            or row["mount"].startswith("/Volumes/")]
    if not kept:
        raise ValueError("df -kPl listed no data volume")
    return kept


def _worktrees(walk: Walk, path: Path) -> list[dict]:
    """The repository's linked worktrees (the first entry is the checkout itself) with their HEAD and branch."""
    listed = walk.git(path, "worktree", "list", "--porcelain")
    if listed is None or listed.returncode != 0:
        return []
    trees, current = [], {}
    for line in listed.stdout.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return [{"path": tree["worktree"], "head": tree.get("HEAD", ""), "branch": tree.get("branch", "").removeprefix("refs/heads/")}
            for tree in trees[1:] if tree.get("worktree")]


def _build_output(walk: Walk, connection: sqlite3.Connection) -> dict:
    checked, merged, unread = 0, [], []
    for key in _registered(connection):
        path = paths.disk(key)
        state, _ = _default(walk, path)
        if state != "ok":
            continue
        for tree in _worktrees(walk, path):
            if not os.path.isdir(tree["path"]):
                continue
            checked += 1
            found = [name for name in BUILD_DIRS if os.path.isdir(os.path.join(tree["path"], name))]
            if not found or not tree["head"]:
                continue
            ancestor = walk.git(path, "merge-base", "--is-ancestor", tree["head"], "refs/remotes/origin/HEAD")
            if ancestor is None or ancestor.returncode not in (0, 1):
                unread.append(tree["path"])
            elif ancestor.returncode == 0:
                merged.append({**tree, "repo": key, "dirs": found})
    return {"checked": checked, "merged": merged, "unread": unread}


def _storage(walk: Walk, roots: list[Path]) -> list[dict]:
    """One entry per configured folder: its subfolders by size, or the reason they were not measured."""
    out = []
    for root in roots:
        entry = {"root": str(root), "folders": [], "error": ""}
        if not root.is_dir():
            entry["error"] = "the folder does not exist or is not mounted"
        elif walk.left() <= 0:
            entry["error"] = "not measured: the Disk budget was spent before this folder"
        else:
            done = walk.run(["du", "-k", "-d", "1", str(root)], spare=True)
            if done is None:
                entry["error"] = "du did not finish inside the Disk budget"
            else:
                for line in done.stdout.splitlines():
                    size, _, name = line.partition("\t")
                    if size.isdigit() and name and Path(name) != root:
                        entry["folders"].append({"path": name, "kb": int(size)})
                entry["folders"].sort(key=lambda folder: -folder["kb"])
                if not entry["folders"] and done.returncode != 0:
                    entry["error"] = (done.stderr.strip().splitlines() or [f"du exited {done.returncode}"])[-1]
        out.append(entry)
    return out


def disk_scan(connection: sqlite3.Connection, *, within: float = DISK_SECONDS, config: Path | None = None) -> dict:
    """Volume use, each configured storage folder's sizes and merged worktrees that keep build output."""
    walk = Walk(within, "the Disk reading")
    volumes = _volumes(walk)
    build = _build_output(walk, connection)
    roots, refused, read = storage_roots(config)
    return {"volumes": volumes, "build": build, "storage": _storage(walk, roots), "refused": refused,
            "config": str(read)}
