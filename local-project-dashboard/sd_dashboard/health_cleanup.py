"""Health's three cleanup writes (sd:3393): prune registrations, delete merged branches, remove build output.

The page posts `/api/health/cleanup` with the row it showed and that row's `revision`. The row names the action:

  gone:<checkout>   `git -C <checkout> worktree prune -v`, for a checkout under the fleet root whose registrations
                    point at a directory that is gone. Its subject is every registration that is not live.
  br:<registered>   `git -C <repo> branch -d <name>`, once per merged branch, for a registered repository.
                    Its subject is each deletable branch by name and tip.
  build:merged      removes `target/`, `node_modules/` and `.venv/` from registered worktrees that are merged into
                    origin/HEAD and clean. Its subject is each such worktree's path, head, status and folders.

Each write reads its subject again first and refuses a digest that differs from the page's (409): evidence from
another row, or a subject that changed since the read, writes nothing. Then each step keeps git's own guard:
`worktree prune` leaves a locked or present directory's registration, and `branch -d` refuses a branch that is not
merged into its upstream or HEAD. Build output is removed only after the worktree is read again, just before its
folders go: the same head, still in origin/HEAD, and `git status --porcelain` empty.

Outcomes follow `queue_screen.move`: a refusal that wrote nothing is a `ValueError` (400) or a `StaleItem` (409), and
so is a write where every step was refused. A step whose result is not known (a git call that did not answer, a folder
removal that stopped part way, a check before or after a step that could not be read) answers 502 with
`outcome: unknown` and what did land; the page reads Health again. When some steps landed, a step git or a check
refused is a definite answer: the write answers 200 and names it, with its reason.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from sd_db import reads

from . import health_collectors
from .health_collectors import Walk

__all__ = ["request"]

#: A write's budget for the git reads and calls around it; removing a folder is not bounded by it.
WRITE_SECONDS = 30.0


def _unknown(error: str, **landed) -> tuple[int, dict]:
    return 502, {"error": error, "outcome": "unknown", **landed}


class _Unread(Exception):
    """A check before a step that could not be read: the step's outcome is not known, never a refusal."""


def _done(row: str, done: list, refused: list, verb: str) -> dict:
    """200 when a step landed; a write where every step was refused wrote nothing, so it is a 400 naming why."""
    if not done and refused:
        raise ValueError(f"Nothing was {verb}: " + "; ".join(f"{x['name']} ({x['why']})" for x in refused))
    return {"ok": True, "row": row, "done": done, "refused": refused}


def _stale(what: str):
    from sd_db import workflow

    return workflow.StaleItem(f"{what} changed since the page read it. Nothing was changed. Read Health again, then retry.")


def _checkout(name: str) -> Path:
    """The checkout `gone:<name>` names: one the fleet lists under its root, never an arbitrary path."""
    from . import fleet

    root = fleet._collectors().REPO_ROOT
    for group, repo in fleet.checkouts(root):
        if (repo.name if group == "." else f"{group}/{repo.name}") == name:
            return repo
    raise ValueError(f"{name} is not a checkout under {root}. Nothing was pruned.")


def prune(name: str, expected: str):
    from . import fleet

    try:
        path = _checkout(name)
        trees = fleet.read_worktrees(".", path)
    except OSError as error:
        raise ValueError(f"The registrations of {name} were not read: {error}. Nothing was pruned.") from None
    if health_collectors.registrations_revision(name, trees) != expected:
        raise _stale(f"The registrations of {name}")
    gone = [tree["name"] for tree in trees if tree["state"] == "abandoned"]
    unread = [tree["name"] for tree in trees if tree["state"] == "unknown"]
    if unread:
        # git judges an unreadable registration by its own rules and may prune it; the page never showed it as gone.
        raise ValueError(f"{len(unread)} registrations of {name} could not be read, and git worktree prune would judge "
                         f"them itself: {', '.join(unread)}. Nothing was pruned.")
    if not gone:
        raise _stale(f"The registrations of {name}")
    try:
        answer = Walk(WRITE_SECONDS, "git worktree prune").git(path, "worktree", "prune", "-v")
    except reads.OverBudget:
        answer = None
    try:
        after = {tree["name"] for tree in fleet.read_worktrees(".", path)}
    except OSError as error:
        return _unknown(f"git worktree prune ran, but the registrations were not read after it: {error}")
    pruned, kept = [n for n in gone if n not in after], [n for n in gone if n in after]
    if answer is None and kept:
        # git did not answer, so why it kept these, and whether it finished, is not known.
        return _unknown("git worktree prune gave no answer in time", done=pruned, refused=[{"name": n, "why": "not known"}
                                                                                          for n in kept])
    why = (answer.stderr.strip() if answer is not None else "") or "git kept it"
    return _done(f"gone:{name}", pruned, [{"name": n, "why": why} for n in kept], "pruned")


def delete_branches(connection, key: str, expected: str):
    from sd_db import paths, repos

    if key not in {row["path"] for row in repos.registered(connection)}:
        raise ValueError(f"{key} is not a registered repository. Nothing was deleted.")
    path = paths.disk(key)
    walk = Walk(WRITE_SECONDS, "the merged-branch delete")
    state, deletable, _ = health_collectors.merged_branches(walk, path)
    if state != "ok":
        raise ValueError(f"The merged branches of {key} were not read ({state}). Nothing was deleted.")
    if health_collectors.branches_revision(key, deletable) != expected:
        raise _stale(f"The merged branches of {key}")
    done, refused = [], []
    for name, _, _ in deletable:
        if name.startswith("-"):
            refused.append({"name": name, "why": "a branch name that starts with - is not passed to git"})
            continue
        step = Walk(WRITE_SECONDS, f"git branch -d {name}")
        try:
            answer = step.git(path, "branch", "-d", name)
            # The ref, not the exit, says what happened: a timed-out or failed call may still have deleted it.
            there = step.git(path, "rev-parse", "-q", "--verify", f"refs/heads/{name}")
        except reads.OverBudget:
            there = None
        if there is None or there.returncode not in (0, 1):
            return _unknown(f"git branch -d {name} ran, but the branch was not read after it", done=done, refused=refused)
        if there.returncode == 1:
            done.append(name)
        else:
            said = answer.stderr.strip().splitlines() if answer is not None else ["git gave no answer in time"]
            # git's reason is its `error:` line; the hints after it say how to force, which the page never does.
            refused.append({"name": name, "why": next((line for line in said if line.startswith("error:")), said[-1] if said
                                                      else "git refused it")})
    return _done(f"br:{key}", done, refused, "deleted")


def _still_removable(walk: Walk, tree: dict) -> str:
    """'' when the worktree is still at its head, in origin/HEAD and clean; otherwise why it is kept.

    A check that could not be read raises `_Unread`: not knowing is not a reason to keep, nor to remove.
    """
    where = Path(tree["path"])
    head = walk.git(where, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        raise _Unread("its head was not read")
    if head.stdout.strip() != tree["head"]:
        return "its head moved since the read"
    ancestor = walk.git(Path(tree["repo_disk"]), "merge-base", "--is-ancestor", tree["head"], "refs/remotes/origin/HEAD")
    if ancestor is None or ancestor.returncode not in (0, 1):
        raise _Unread("whether it is in origin/HEAD was not read")
    if ancestor.returncode == 1:
        return "it is no longer in origin/HEAD"
    status = walk.git(where, "status", "--porcelain")
    if status is None or status.returncode != 0:
        raise _Unread("its status was not read")
    return "it has uncommitted changes" if status.stdout else ""


def remove_build_output(connection, expected: str):
    from sd_db import paths, repos

    keys = [row["path"] for row in repos.registered(connection)]
    try:
        merged = health_collectors.build_output(Walk(WRITE_SECONDS, "the build output read"), keys)["merged"]
    except reads.OverBudget as error:
        raise ValueError(f"The worktrees were not read: {error}. Nothing was removed.") from None
    if health_collectors.build_revision(merged) != expected:
        raise _stale("The merged worktrees with build output")
    done, refused = [], []
    for tree in merged:
        try:
            # Read again just before this worktree's folders go: a step that moved since the read above keeps them.
            kept = "it has uncommitted changes" if tree["status"] else _still_removable(
                Walk(WRITE_SECONDS, "the worktree check"), {**tree, "repo_disk": str(paths.disk(tree["repo"]))})
        except (reads.OverBudget, _Unread) as error:
            return _unknown(f"{tree['path']} was not checked again before its folders went ({error}); it and the "
                            "worktrees after it were left", done=done, refused=refused)
        if kept:
            refused.append({"name": tree["path"], "why": kept})
            continue
        for name in tree["dirs"]:
            folder = Path(tree["path"]) / name
            if name not in health_collectors.BUILD_DIRS or folder.is_symlink() or not folder.is_dir():
                refused.append({"name": str(folder), "why": "not a build output folder"})
                continue
            try:
                shutil.rmtree(folder)
            except OSError as error:
                return _unknown(f"{folder} was removed only in part: {error}", done=done, refused=refused)
            done.append(str(folder))
    return _done("build:merged", done, refused, "removed")


def request(payload: dict):
    """Check the page's request, then return the write: `action_route`'s contract."""
    values = dict(payload)
    if (set(values) != {"row", "revision"} or not isinstance(values["row"], str) or len(values["row"]) > 1000
            or not isinstance(values["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", values["revision"])):
        raise ValueError("Name the Health row and the revision the page showed for it.")
    row, expected = values["row"], values["revision"]
    if row.startswith("gone:") and row[5:]:
        return lambda connection: prune(row[5:], expected)
    if row.startswith("br:") and row[3:]:
        return lambda connection: delete_branches(connection, row[3:], expected)
    if row == "build:merged":
        return lambda connection: remove_build_output(connection, expected)
    raise ValueError("Only a registrations, merged branches or build output row has a cleanup.")
