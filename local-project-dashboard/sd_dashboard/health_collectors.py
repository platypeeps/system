"""The Disk and Branches readers behind Health (sd:2202, sd:2204).

Both read this machine at request time and write nothing, as the other
Health readers do. Each runs inside one overall budget: a command that would
end past it is stopped, and the reader raises `reads.OverBudget`, which Health shows as the
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
  `du` of every tree, which the budget does not hold. Each such tree carries
  its `git status --porcelain`: only a clean one is offered for removal.

**Doctor** (sd:3393) is `machine-setup.sh doctor`, run as a child under the
collectors' `Budget`, which kills its whole process group at the deadline.
Its `ok`, `WARN`, `FAIL` and `--` lines are parsed as they are printed; an
exit or a last line the doctor does not end with is the area's error, so a
run that failed or was stopped never reads as clean.

**Revisions.** Each subject a cleanup acts on has a digest, `revision`, of
what the page showed: a checkout's registrations whose directory is gone,
a repository's merged branches and their tips, and the merged worktrees
with build output, each with its head and status. `health_cleanup` reads
the subject again at the write and refuses a digest that changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from sd_db import paths, reads, repos

from . import documents

__all__ = ["BRANCH_SECONDS", "DISK_SECONDS", "DOCTOR_SECONDS", "branch_scan", "disk_scan", "doctor_scan", "parse_doctor",
           "revision", "storage_roots"]

#: Each reader's overall budget. Branches is two git calls per registered repository; Disk is df, a few git calls per repository and the du walks, which get what is left.
BRANCH_SECONDS = 8.0
DISK_SECONDS = 8.0
#: One command's own ceiling inside the budget.
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


def revision(subject) -> str:
    """The digest of what a row showed, the one the page sends back with a cleanup."""
    return hashlib.sha256(json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def registrations_revision(name: str, trees: list[dict]) -> str:
    """The `gone:<name>` row's subject: the checkout's registrations that are not live (gone or unreadable)."""
    return revision([f"gone:{name}", sorted([tree.get("name") or "", tree.get("path") or "", tree.get("branch") or "",
                                             tree["state"]] for tree in trees if tree["state"] != "live")])


def build_revision(merged: list[dict]) -> str:
    """Merged worktrees with build output: each path, head, status digest and folders."""
    return revision(sorted([tree["path"], tree["head"], hashlib.sha256(tree["status"].encode("utf-8")).hexdigest(),
                            list(tree["dirs"])] for tree in merged))


def _registered(connection: sqlite3.Connection, repo_paths: list[str] | None) -> list[str]:
    """The paths the caller read, or the registry's. Health reads it on its own thread and passes the paths."""
    return list(repo_paths) if repo_paths is not None else [row["path"] for row in repos.registered(connection)]


def _default(walk: Walk, path: Path) -> tuple[str, str | None]:
    """('ok', branch name) for a repository with origin/HEAD; ('no_default', None) or ('unread', None) otherwise."""
    found = walk.git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if found is None or found.returncode not in (0, 1):
        return "unread", None
    if found.returncode == 1 or not found.stdout.strip():
        return "no_default", None
    return "ok", found.stdout.strip().split("/", 1)[-1]


def merged_branches(walk: Walk, path: Path) -> tuple[str, list[tuple[str, str, str]], list[tuple[str, str]]]:
    """('ok', deletable, checked out) for a repository with origin/HEAD, or ('no_default' | 'unread', [], []).

    A deletable branch is (name, committer date, tip); a checked-out one is (name, worktree path).
    """
    state, default = _default(walk, path)
    if state != "ok":
        return state, [], []
    refs = walk.git(path, "for-each-ref", "--merged=refs/remotes/origin/HEAD",
                    "--format=%(refname)%09%(committerdate:short)%09%(worktreepath)%09%(objectname)", "refs/heads/")
    if refs is None or refs.returncode != 0:
        return "unread", [], []
    deletable, checked_out = [], []
    for line in refs.stdout.splitlines():
        ref, date, tree, tip = (line.split("\t") + ["", "", ""])[:4]
        # The exact branch name: %(refname:short) prints heads/foo or refs/heads/foo when a tag or branch shares it.
        name = ref.removeprefix("refs/heads/")
        if not name or name == default:
            continue
        (checked_out.append((name, tree)) if tree else deletable.append((name, date, tip)))
    return "ok", deletable, checked_out


def branches_revision(key: str, deletable: list) -> str:
    """The `br:<key>` row's subject: the repository's deletable merged branches by name and tip.

    The row id is part of it: two clones with the same branches at the same tips must not share a revision.
    """
    return revision([f"br:{key}", sorted([name, tip] for name, _, tip in deletable)])


def branch_scan(connection: sqlite3.Connection, *, within: float = BRANCH_SECONDS,
                repo_paths: list[str] | None = None) -> dict:
    """Each registered repository's local branches already in `origin/HEAD`, and the repositories not read."""
    walk = Walk(within, "the merged-branch walk")
    scan: dict = {"repos": 0, "merged": [], "no_default": [], "unread": []}
    for key in _registered(connection, repo_paths):
        path = paths.disk(key)
        state, deletable, checked_out = merged_branches(walk, path)
        if state != "ok":
            scan[state].append(key)
            continue
        scan["repos"] += 1
        if deletable or checked_out:
            scan["merged"].append({"repo": key, "path": str(path), "deletable": [(name, date) for name, date, _ in deletable],
                                   "checked_out": checked_out, "revision": branches_revision(key, deletable)})
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
    except (OSError, UnicodeDecodeError) as error:
        # A file that is there but cannot be read is a named line, as on Now and Home, not the whole area's error.
        return roots, [f"{config} could not be read: {error.__class__.__name__}: {error}"], config
    for number, line, _ in documents.pipe_lines(text):
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
        # A pseudo file system can print "-" for its sizes or its capacity: it is not a volume.
        if len(fields) < 6 or not all(field.isdigit() for field in (*fields[1:4], fields[4].rstrip("%"))):
            continue
        mount = " ".join(fields[5:])
        rows.append({"filesystem": fields[0], "size_kb": int(fields[1]), "used_kb": int(fields[2]),
                     "avail_kb": int(fields[3]), "capacity": int(fields[4].rstrip("%")), "mount": mount})
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


def build_output(walk: Walk, keys: list[str]) -> dict:
    """Registered worktrees merged into origin/HEAD that keep build output, each with its `git status --porcelain`."""
    checked, merged, unread = 0, [], []
    for key in keys:
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
                status = walk.git(Path(tree["path"]), "status", "--porcelain")
                if status is None or status.returncode != 0:
                    unread.append(tree["path"])
                else:
                    merged.append({**tree, "repo": key, "dirs": found, "status": status.stdout})
    return {"checked": checked, "merged": merged, "unread": unread, "revision": build_revision(merged)}


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


def disk_scan(connection: sqlite3.Connection, *, within: float = DISK_SECONDS, config: Path | None = None,
              repo_paths: list[str] | None = None) -> dict:
    """Volume use, each configured storage folder's sizes and merged worktrees that keep build output."""
    keys = _registered(connection, repo_paths)
    walk = Walk(within, "the Disk reading")
    volumes = _volumes(walk)
    build = build_output(walk, keys)
    roots, refused, read = storage_roots(config)
    return {"volumes": volumes, "build": build, "storage": _storage(walk, roots), "refused": refused,
            "config": str(read)}


#: The doctor's deadline: it runs `brew doctor`, which takes tens of seconds on a quiet machine.
DOCTOR_SECONDS = 180.0
MACHINE_SETUP = Path(__file__).resolve().parents[2] / "local-machine-setup" / "machine-setup.sh"
#: One finding: two or four spaces, a marker, then the text. A line indented past the marker column continues the one
#: before; a heading such as "== doctor" or "  folders without a filled .env" is context, kept with the passing lines.
DOCTOR_LINE = re.compile(r"^ {2,4}(ok|WARN|FAIL|SKIP|--) +(\S.*)$")
DOCTOR_MORE = re.compile(r"^ {6,}(\S.*)$")
DOCTOR_FAILS = re.compile(r"^  (\d+) hard failure\(s\)$")


def parse_doctor(text: str, code: int) -> dict:
    """The doctor's findings, in order, and its count of hard failures.

    The doctor ends with "no hard failures" and exit 0, or "N hard failure(s)" and exit 1. Any other end is a run
    that did not finish, a `die` or a doctor this parser does not know: a `ValueError`, never an empty clean list.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    counted = DOCTOR_FAILS.match(last)
    if not ((code == 0 and last == "  no hard failures") or (code == 1 and counted)):
        raise ValueError(f"machine-setup.sh doctor exited {code} without its closing line"
                         + (f": {last.strip()}" if last.strip() else ""))
    checks, other = [], []
    for line in lines[:-1]:
        found, more = DOCTOR_LINE.match(line), DOCTOR_MORE.match(line)
        if found:
            checks.append({"mark": found[1], "text": found[2], "more": []})
        elif more and checks:
            checks[-1]["more"].append(more[1])
        elif not line.startswith("== "):
            other.append(line.strip())
    return {"checks": checks, "other": other, "fails": int(counted[1]) if counted else 0}


def doctor_scan(*, script: Path | None = None, within: float = DOCTOR_SECONDS) -> dict:
    """`machine-setup.sh doctor` in a child process group, killed at `within` seconds, and its parsed lines."""
    from . import fleet

    collectors = fleet._collectors()
    budget = collectors.Budget(within)
    try:
        done = budget.run([str(script or MACHINE_SETUP), "doctor"], label="doctor")
    except collectors.OverBudget as error:
        raise reads.OverBudget(f"machine-setup.sh doctor was stopped: {error}") from None
    except OSError as error:
        raise ValueError(f"machine-setup.sh doctor did not start: {error}") from None
    try:
        return parse_doctor(done.stdout, done.returncode)
    except ValueError as error:
        why = (done.stderr or "").strip().splitlines()
        raise ValueError(f"{error}{f' ({why[-1]})' if why else ''}") from None
