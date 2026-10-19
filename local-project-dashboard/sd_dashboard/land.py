"""Land (sd:5104): one action on a checkout's Management row lands its uncommitted and unpushed work through the lane.

Management reads `GET /api/repos/land/preview?path=<key>&items=<spec>` (`preview`) and shows what Land would do; the operator
confirms, and the page posts `/api/repos/land` with that preview's `revision` (`request`). Nothing moves on a read.

What lands: every uncommitted path in the checkout (`git status`, untracked included, ignored left out) and every commit
on HEAD that origin's default branch lacks. The items come from `sd-<n>` in the branch name and `sd:<n>` in those
commits, or from what the operator types: `5104` puts every path in one commit for sd:5104; `5104: lib/ docs/; 5105`
splits by path prefix, longest first, and the one item without a prefix takes the rest. Each path lands in exactly one
item's commit, or the preview names the paths no item takes and Land refuses.

The steps of `land`, each named so a later run can find what an earlier one left:

1. Commits. One commit per item, built in a temporary index from the checkout's files on top of HEAD, so neither the
   checkout's index nor its branch moves yet. Each message is the item's title, `(sd:<n>)`, and only the line
   `Authored-with: claude/anthropic`.
2. The branch. On a feature branch, `update-ref` moves it from the HEAD the preview read to the last commit (a moved
   HEAD refuses), then `reset -q` refreshes the index: the files already match. On the default branch, or a detached
   HEAD, Land never commits there: it makes `land/sd-<first item>` at the last commit and a worktree for it under
   `~/worktrees/<checkout>-land-sd-<first item>`, then restores each landed path in the checkout to HEAD, but only a path
   whose file still matches the branch, and moves an ahead default branch back to origin's with `reset --keep`, once the
   branch holds its commits. The lane later merges from the new worktree; the default branch is never pushed.
3. Enqueue. `sd-ship -C <worktree> lane enqueue --item … --title … --body-file … --expected-head <sha> --deliver`
   (or `--associate-only`), the lane's own checks and publish. Its answer is the lane state the page shows.

A run that stopped after step 1 left only objects: nothing to undo. One that stopped after the `land/sd-<n>` branch was
made is resumed: the preview names it, and Land skips step 1, adds the worktree if it is missing, restores what still
matches, moves the default branch back if it still holds the commits, and enqueues. An enqueue the lane refused (exit 3
with a refusal) left the commits; the answer says so and a second Land enqueues again.

Land reads the files, so it refuses first where another view of the tree disagrees with them (`_settled`): a staged
version the file no longer holds, an unmerged path, a merge or rebase in progress. A resume needs evidence that the
branch is this landing: it holds HEAD and every current change, and the lane has not merged it. Every item must take a
path or be named by a commit the lane gets, or the lane would take an item with nothing landed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

__all__ = ["TRAILER", "parse_spec", "preview", "request"]

TRAILER = "Authored-with: claude/anthropic"
GIT_SECONDS = 30
ENQUEUE_SECONDS = 180
#: Item refs: `sd-5104` or `sd5104` in a branch name, `sd:5104` in a commit message.
BRANCH_REF = re.compile(r"(?:^|[-_/])sd-?([1-9][0-9]{0,8})(?=$|[-_/])", re.ASCII)
COMMIT_REF = re.compile(r"\bsd:([1-9][0-9]{0,8})\b", re.ASCII)
CLAIMS = ("deliver", "associate-only")
#: Status output whose shape no config changes, as sd-ship's cleanup pins it.
PINNED = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "status.showUntrackedFiles=all",
          "-c", "core.quotePath=false")
#: What an operation in progress leaves in the git dir; a commit over it would drop the operation's own state.
BUSY = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


class Unknown(Exception):
    """A step whose result is not known: the write answers 502 with what did land."""


def _git(path: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=GIT_SECONDS,
                              check=False, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unknown(f"git {args[0]} gave no answer: {error}") from None
    if check and done.returncode != 0:
        raise Unknown(f"git {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done


def _out(path: Path, *args: str) -> str:
    return _git(path, *args).stdout.strip()


def _ref(path: Path, name: str) -> str | None:
    found = _git(path, "rev-parse", "-q", "--verify", f"{name}^{{commit}}", check=False)
    return found.stdout.strip() if found.returncode == 0 else None


def parse_spec(spec: str) -> list[tuple[int, list[str]]]:
    """`5104: lib/ docs/; 5105` as `[(5104, ['lib/', 'docs/']), (5105, [])]`; `ValueError` names what is wrong."""
    groups: list[tuple[int, list[str]]] = []
    for part in (piece.strip() for piece in spec.split(";")):
        if not part:
            continue
        number, _, rest = re.sub(r"^sd[:-]?", "", part).partition(":")
        number = number.strip()
        if not re.fullmatch(r"[1-9][0-9]{0,8}", number):
            raise ValueError(f"{part!r} names no item: write 5104, or 5104: <path prefix> …")
        prefixes = rest.split()
        if any(prefix.startswith(("/", "-")) or ".." in Path(prefix).parts for prefix in prefixes):
            raise ValueError(f"{part!r}: a path prefix is relative to the checkout and stays inside it")
        if int(number) in (item for item, _ in groups):
            raise ValueError(f"sd:{number} is named twice")
        groups.append((int(number), prefixes))
    if not groups:
        raise ValueError("Name at least one item.")
    if sum(1 for _, prefixes in groups if not prefixes) > 1:
        raise ValueError("Only one item may take the rest: give every other item a path prefix (5104: lib/; 5105).")
    return groups


def _changes(path: Path) -> list[str]:
    """Every uncommitted path, untracked included; a rename is both its paths."""
    raw = _git(path, *PINNED, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    found, fields = [], raw.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        found.append(entry[3:])
        if entry[0] in "RC":  # the source path follows as its own field
            found.append(fields[index])
            index += 1
    return sorted(set(found))


def _z(path: Path, *args: str) -> list[str]:
    return [name for name in _git(path, *PINNED, *args).stdout.split("\0") if name]


def _settled(path: Path, depth: int) -> None:
    """Refuse a tree whose index or git dir holds work its files do not: Land ships the files, then refreshes the index.

    A staged version the file no longer holds, a staged delete of a file still on disk, an unmerged path or an
    operation in progress would be lost or committed over. An index that is the whole tree of one of the `depth`
    commits before HEAD holds nothing new: an in-place Land stopped before `reset -q` left it.
    """
    unmerged = sorted(set(_z(path, "diff", "--name-only", "-z", "--diff-filter=U")))
    if unmerged:
        raise ValueError(f"{', '.join(unmerged[:6])}: unmerged; finish or abort the merge first. Nothing was landed.")
    busy = [name for name in BUSY if (path / _out(path, "rev-parse", "--git-path", name)).exists()]
    if busy:
        raise ValueError(f"An operation is in progress ({', '.join(busy)}): finish or abort it first. Nothing was landed.")
    staged = set(_z(path, "diff", "--cached", "--name-only", "-z", "--no-renames"))
    loose = set(_z(path, "diff", "--name-only", "-z", "--no-renames")) | set(_z(path, "ls-files", "-z", "--others",
                                                                                "--exclude-standard"))
    apart = sorted(staged & loose)
    if apart and _out(path, "write-tree") not in _out(path, "log", f"-{depth}", "--skip=1", "--format=%T", "HEAD").split():
        names = " ".join(apart[:6])
        raise ValueError(f"The index and the file differ for {names}: Land ships the files, so the staged version would "
                         f"be lost. git add -- {names} lands the files; git restore --staged -- {names} drops the staged "
                         "version. Nothing was landed.")


def _assign(groups: list[tuple[int, list[str]]], changed: list[str]) -> tuple[dict[int, list[str]], list[str]]:
    """Each path to the item with its longest matching prefix, else to the item without one; and the paths none takes."""
    rest = next((item for item, prefixes in groups if not prefixes), None)
    out: dict[int, list[str]] = {item: [] for item, _ in groups}
    left = []
    for name in changed:
        best = max(((len(prefix), item) for item, prefixes in groups for prefix in prefixes
                    if name == prefix.rstrip("/") or name.startswith(prefix.rstrip("/") + "/")), default=None)
        owner = best[1] if best else rest
        (out[owner] if owner is not None else left).append(name)
    return out, left


def _registered(connection, key: str) -> Path:
    from sd_db import paths, repos

    row = next((row for row in repos.registered(connection) if row["path"] == key), None)
    if row is None:
        raise ValueError(f"{key} is not a registered repository. Nothing was landed.")
    return paths.disk(row["path"])


def _items(connection, numbers) -> dict[int, str]:
    """Each item's title; a missing or done item refuses."""
    from sd_db import workflow

    titles = {}
    for number in numbers:
        try:
            row = workflow.item_state(connection, number)["item"]
        except Exception as error:  # noqa: BLE001 - the library's own sentence names why
            raise ValueError(f"sd:{number}: {error}") from None
        if row["status"] == "done":
            raise ValueError(f"sd:{number} is done; Land commits only for an open item.")
        titles[number] = " ".join(str(row["title"]).split())
    return titles


def _names(path: Path, first: int) -> tuple[str, Path]:
    return f"land/sd-{first}", Path.home() / "worktrees" / f"{path.name}-land-sd-{first}"


def _read(connection, key: str, spec: str | None) -> dict:
    """The facts a preview shows and a write checks again; refusals are `ValueError`."""
    path = _registered(connection, key)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this machine. Nothing was landed.")
    origin = _git(path, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).stdout.strip()
    if not origin:
        raise ValueError(f"{path} has no origin/HEAD, so its default branch is not known: "
                         f"git -C {path} remote set-head origin --auto. Nothing was landed.")
    default = origin.split("/", 1)[1]
    branch = _out(path, "branch", "--show-current")
    head = _out(path, "rev-parse", "HEAD")
    ahead = [line.split("\x1f") for line in _out(path, "log", "--reverse", "--format=%H\x1f%s\x1f%b\x1e",
                                                  f"{origin}..HEAD").split("\x1e") if line.strip()]
    commits = [{"sha": fields[0].strip(), "subject": fields[1]} for fields in ahead]
    found = list(dict.fromkeys([int(n) for n in BRANCH_REF.findall(branch)]
                               + [int(n) for fields in ahead for n in COMMIT_REF.findall(" ".join(fields[1:]))]))
    suggested = "; ".join(str(n) for n in found)
    changed = _changes(path)
    facts = {"path": key, "disk": str(path), "branch": branch, "default": default, "head": head, "commits": commits,
             "changed": changed, "suggested": suggested, "spec": suggested if spec is None else spec.strip()}
    on_default = branch in ("", default)
    if facts["spec"]:
        groups = parse_spec(facts["spec"])
    else:
        groups = []
    first = groups[0][0] if groups else None
    land_branch, worktree = _names(path, first) if first else (None, None)
    tip = _ref(path, f"refs/heads/{land_branch}") if on_default and land_branch else None
    resume = bool(tip)
    facts.update(mode="resume" if resume else "branch" if on_default else "in place", tip=tip,
                 land_branch=land_branch if on_default else branch, worktree=str(worktree) if on_default and worktree else str(path))
    _settled(path, max(1, len(groups)))
    if not changed and not commits and not resume:
        raise ValueError(f"{key} has no uncommitted change and no commit that origin/{default} lacks: nothing to land.")
    if not groups:
        facts.update(groups=[], left=changed, titles={})
        return facts
    if resume:
        # The branch existing is not evidence of this landing: it must hold HEAD and every current change.
        if _git(path, "merge-base", "--is-ancestor", head, tip, check=False).returncode != 0:
            raise ValueError(f"{land_branch} does not hold this checkout's HEAD {head[:12]}; it is another landing. "
                             f"Remove it or name another item first. Nothing was landed.")
        later = [name for name in changed if not _matches(path, tip, name)]
        if later:
            raise ValueError(f"{', '.join(later[:6])} changed after {land_branch} was made, and Land enqueues that branch "
                             f"as it is. Set them aside (git stash -u), Land, then land them under another item. "
                             f"Nothing was landed.")
    shipped = tip if resume else head if facts["mode"] == "in place" and not changed else None
    if shipped:
        from .health_collectors import merged_heads

        if (os.path.realpath(facts["worktree"]), shipped) in merged_heads(connection):
            raise ValueError(f"The lane already merged {shipped[:12]} from {facts['worktree']}: remove "
                             f"{facts['land_branch']} (Health > Checkouts) before Land runs for it again. Nothing was landed.")
    assigned, left = _assign(groups, changed if not resume else [])
    # An item with no path and no commit naming it would be closed by the lane with nothing landed.
    named = {int(n) for n in COMMIT_REF.findall(_out(path, "log", "--format=%B", f"{origin}..{tip or head}"))}
    if facts["mode"] == "in place":
        named |= {int(n) for n in BRANCH_REF.findall(branch)}
    idle = [item for item, _ in groups if not assigned[item] and item not in named]
    if idle:
        raise ValueError(f"sd:{idle[0]} takes no changed path and no commit names it, so the lane would take it with "
                         f"nothing landed. Name only the items with work. Nothing was landed.")
    facts["titles"] = _items(connection, [item for item, _ in groups])
    facts["groups"] = [{"item": item, "title": facts["titles"][item], "paths": assigned[item]} for item, _ in groups]
    facts["left"] = left
    return facts


def revision(facts: dict) -> str:
    """What the preview showed and the write must find again: the checkout, its HEAD, its changes and the plan."""
    subject = [facts["path"], facts["branch"], facts["head"], [c["sha"] for c in facts["commits"]], facts["changed"],
               facts["spec"], facts["mode"], facts["tip"], facts.get("groups", [])]
    return hashlib.sha256(json.dumps(subject, sort_keys=True).encode()).hexdigest()


def _title(facts: dict) -> str:
    groups = facts["groups"]
    refs = ", ".join(f"sd:{g['item']}" for g in groups)
    return f"{groups[0]['title']} ({refs})" if len(groups) == 1 else f"{groups[0]['title']} and {len(groups) - 1} more ({refs})"


def _body(facts: dict) -> str:
    lines = ["## Summary", ""]
    for group in facts["groups"]:
        files = len(group["paths"])
        lines.append(f"- sd:{group['item']}: {group['title']}." + (f" {files} {'file' if files == 1 else 'files'} landed "
                                                                     "from the checkout." if files else ""))
    for commit in facts["commits"]:
        lines.append(f"- Carries {commit['sha'][:12]}: {commit['subject']}")
    lines += ["", "## Test plan", "", "- Landed with the dashboard's Land action; the lane's local gate runs "
              "`make check` before the merge.", ""]
    return "\n".join(lines)


def _enqueue_argv(facts: dict, head: str, claim: str, body_file: str) -> list[str]:
    from .queue_screen import command

    argv = [command("sd-ship"), "-C", facts["worktree"], "lane", "enqueue"]
    for group in facts["groups"]:
        argv += ["--item", str(group["item"])]
    return argv + ["--title", _title(facts), "--body-file", body_file, "--expected-head", head, f"--{claim}"]


def _cli(facts: dict, claim: str = "deliver") -> str:
    import shlex

    if not facts.get("groups"):
        return ""
    argv = _enqueue_argv(facts, "<head>", claim, "<body file>")
    return " ".join(["sd-ship", *map(shlex.quote, argv[1:])])


def preview(connection, parameters: dict) -> dict | tuple[int, dict]:
    """`GET /api/repos/land/preview?path=<key>[&items=<spec>]`: what Land would do, and the revision its write must match."""
    key = (parameters.get("path") or [""])[0] if isinstance(parameters.get("path"), list) else parameters.get("path")
    spec = parameters.get("items")
    spec = spec[0] if isinstance(spec, list) else spec
    if not key:
        return 400, {"error": "Name the checkout: path=<key>."}
    try:
        facts = _read(connection, key, spec)
    except ValueError as error:
        return 409, {"error": str(error)}
    except Unknown as error:
        return 502, {"error": f"The checkout was not read: {error}"}
    ready = bool(facts["groups"]) and not facts["left"]
    why = ("" if ready else "Name the item each change lands under." if not facts["groups"]
           else f"No item takes {len(facts['left'])} changed paths: {', '.join(facts['left'][:6])}.")
    return {**facts, "ready": ready, "why": why, "revision": revision(facts), "cli": _cli(facts)}


def _commit_chain(path: Path, parent: str, groups: list[dict], titles: dict[int, str]) -> list[dict]:
    """Step 1: one commit per item with files, from the checkout's files, in a temporary index; no ref moves."""
    made = []
    with tempfile.TemporaryDirectory(prefix="sd-land-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(path, "read-tree", parent, env=env)
        for group in groups:
            if not group["paths"]:
                continue
            _git(path, "add", "-A", "--", *group["paths"], env=env)
            tree = _git(path, "write-tree", env=env).stdout.strip()
            if tree == _out(path, "rev-parse", f"{parent}^{{tree}}"):
                continue  # the files already match: a run stopped before `reset -q` left only the index behind
            message = f"{titles[group['item']]} (sd:{group['item']})\n\n{TRAILER}\n"
            done = subprocess.run(["git", "-C", str(path), "commit-tree", tree, "-p", parent, "-F", "-"], input=message,
                                  capture_output=True, text=True, timeout=GIT_SECONDS, check=False, env=env)
            if done.returncode != 0:
                raise Unknown(f"git commit-tree failed: {done.stderr.strip()[-300:]}")
            parent = done.stdout.strip()
            made.append({"item": group["item"], "sha": parent, "paths": group["paths"]})
    return made


def _matches(path: Path, tip: str, name: str) -> bool:
    """Whether the checkout's file `name` is what the land branch holds: both absent, or the same blob."""
    there = _git(path, "rev-parse", "-q", "--verify", f"{tip}:{name}", check=False)
    file = path / name
    if not file.exists() and not file.is_symlink():
        return there.returncode != 0
    if there.returncode != 0:
        return False
    return _out(path, "hash-object", "--", name) == there.stdout.strip()


def _blob(path: Path, rev: str) -> str:
    """The object `<commit>:<path>` or `:<path>` (the index) names, or "" when there is none."""
    return _git(path, "rev-parse", "-q", "--verify", rev, check=False).stdout.strip()


def _restore(path: Path, head: str, tip: str, names: list[str]) -> tuple[list[str], list[str]]:
    """Step 2's restore: each landed path back to HEAD in the checkout, only where its file matches the branch and its
    index entry holds nothing else (HEAD's version or the branch's)."""
    restored, kept = [], []
    for name in names:
        if not _matches(path, tip, name) or _blob(path, f":{name}") not in (_blob(path, f"{head}:{name}"),
                                                                            _blob(path, f"{tip}:{name}")):
            kept.append(name)
            continue
        if _git(path, "cat-file", "-e", f"{head}:{name}", check=False).returncode == 0:
            _git(path, "checkout", head, "--", name)
        else:
            _git(path, "rm", "-q", "--cached", "--ignore-unmatch", "--", name)
            (path / name).unlink(missing_ok=True)
        restored.append(name)
    return restored, kept


def land(connection, values: dict):
    facts = _read(connection, values["path"], values["items"])
    if revision(facts) != values["revision"]:
        from sd_db import workflow

        raise workflow.StaleItem("The checkout changed since the preview. Nothing was landed. Open Land again.")
    if not facts["groups"] or facts["left"]:
        raise ValueError("Every change needs an item before Land commits. Nothing was landed.")
    path, head = Path(facts["disk"]), facts["head"]
    done: dict = {"branch": facts["land_branch"], "worktree": facts["worktree"], "mode": facts["mode"], "commits": []}
    try:
        if facts["mode"] == "resume":
            tip = facts["tip"]
        else:
            made = _commit_chain(path, head, facts["groups"], facts["titles"])
            done["commits"] = made
            tip = made[-1]["sha"] if made else head
            if facts["mode"] == "in place":
                _settled(path, len(facts["groups"]))  # staged since the read: the index refresh would drop it
                _git(path, "update-ref", "-m", "land (sd:5104)", f"refs/heads/{facts['branch']}", tip, head)
                _git(path, "reset", "-q")
            else:
                _git(path, "branch", facts["land_branch"], tip)
        done["head"] = tip
        if facts["mode"] != "in place":
            tree = Path(facts["worktree"])
            listed = _out(path, "worktree", "list", "--porcelain")
            if f"branch refs/heads/{facts['land_branch']}" not in listed:
                tree.parent.mkdir(parents=True, exist_ok=True)
                _git(path, "worktree", "add", "-q", str(tree), facts["land_branch"])
            done["restored"], done["kept"] = _restore(path, head, tip, _changes(path))
            if facts["branch"] == facts["default"] and facts["branch"]:
                origin = f"refs/remotes/origin/{facts['default']}"
                now = _out(path, "rev-parse", "HEAD")
                if now != _ref(path, origin) and _git(path, "merge-base", "--is-ancestor", now, tip,
                                                      check=False).returncode == 0:
                    _git(path, "reset", "-q", "--keep", origin)
                    done["reset"] = f"{facts['default']} moved back from {now[:12]} to origin/{facts['default']}"
        # In the try: a worktree or body file the enqueue could not read or write is a stop after the commits landed.
        return _enqueue(facts, tip, values["claim"], done)
    except Unknown as error:
        return 502, {"error": f"Land stopped part way: {error}", "outcome": "unknown", **done}


def _enqueue(facts: dict, head: str, claim: str, done: dict):
    """Step 3: the lane's enqueue; a refusal leaves the commits, and a second Land enqueues again."""
    from .queue_screen import _run, refused

    tree = Path(facts["worktree"])
    gitdir = Path(_out(tree, "rev-parse", "--absolute-git-dir"))
    body = gitdir / "sd-land-body.md"
    try:
        body.write_text(_body(facts), encoding="utf-8")
    except OSError as error:
        raise Unknown(f"the pull-request body {body} was not written: {error}") from None
    answer = _run(_enqueue_argv(facts, head, claim, str(body)), ENQUEUE_SECONDS)
    if answer.get("ok") and answer.get("exit") == 0:
        done["lane"] = {"status": answer.get("status") or "pending", "item": answer.get("item"),
                        "expected_head": answer.get("expected_head") or head}
        return {"ok": True, **done}
    if refused(answer) or answer.get("exit") == 3:
        done["lane"] = {"status": "not queued", "error": answer.get("error") or "sd-ship lane enqueue refused"}
        return {"ok": True, **done}
    return 502, {"error": f"Committed, but whether the lane queued it is not known: {answer.get('error')}",
                 "outcome": "unknown", **done}


def request(payload: dict):
    """`POST /api/repos/land`: the page's checks before the server opens a writer; the write as `action_route` takes it."""
    values = dict(payload)
    if (set(values) != {"path", "items", "revision", "claim"} or not isinstance(values["path"], str)
            or not isinstance(values["items"], str) or len(values["items"]) > 2000
            or not isinstance(values["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", values["revision"])
            or values["claim"] not in CLAIMS):
        raise ValueError("Name the checkout, the items as the preview showed them, its revision, and deliver or "
                         "associate-only.")
    return lambda connection: land(connection, values)
