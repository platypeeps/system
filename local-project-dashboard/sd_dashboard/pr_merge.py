"""Merge (sd:5668): Today > GitHub's Merge copies an open pull request into a worktree and enqueues it in the lane.

The operator's ruling is "copy into the lane": the lane publishes its own pull request with the copied commits, gates,
reviews and merges it, so a pull request someone else opened, Dependabot's too, lands the way the operator's own work
does. Only a pull request `sd shadow sync` saw open in a repository sd registers is taken, and the copy is made in that
repository's registered checkout (`today_github._checkouts`), whose `origin` must name the same repository.

The page reads `GET /api/today/github/merge/preview?repo=<owner/name>&number=<n>` (`preview`) and asks with it: the
pull request, its head as `git ls-remote origin refs/pull/<n>/head` reads it, its author, the worktree and the item.
Nothing moves on that read. The confirm posts `/api/today/github/merge` `{repo, number, revision}` (`request`), and the
write reads every fact again: a revision that differs, such as a head that moved, moves nothing.

The steps of `merge`, each named so a later run can find what an earlier one left:

1. Fetch. `git fetch origin pull/<n>/head` in the registered checkout; `FETCH_HEAD` must be the head the preview read.
2. The branch. `pr/<n>` at that head. No branch of the checkout moves, and the commits keep their authors.
3. The worktree. `~/worktrees/<checkout>-pr-<n>` on `pr/<n>`, outside `~/repos`.
4. The item. The one filed under the ref `github:<owner>/<name>#<n>`, else a new task filed under that ref, as
   `sd task add --ref` files one (`server.capture_ref`).
5. Enqueue. `sd-ship -C <worktree> lane enqueue --item … --title … --body-file … --expected-head <head> --deliver`,
   Land's own step (`land.enqueue`). Its answer is the lane state the page shows.

Failure table (step, state moved, failure, recovery, test in `test_pr_merge.py`):

- Read: nothing. `ls-remote` fails: 502, nothing moved (`test_a_head_that_does_not_read_is_unknown_and_moves_nothing`).
  Not open, not registered, another origin, a done item: 409 (`Refusals`).
- Revision: nothing. The head moved since the preview: `StaleItem`, nothing moved
  (`test_a_head_that_moved_since_the_preview_moves_nothing`).
- 1 Fetch: objects and `FETCH_HEAD` only. The fetch fails: 502, the next Merge fetches again
  (`test_a_fetch_that_failed_moves_nothing_and_the_next_merge_copies`); the head moved between the read and the fetch:
  `StaleItem`, no branch (`test_a_head_that_moved_during_the_fetch_makes_no_branch`).
- 2 Branch: `pr/<n>` at the head. A stop after it: the next preview names it, `resume`, and Merge skips the fetch
  (`test_a_run_stopped_before_the_worktree_adds_it_on_the_next_merge`). A `pr/<n>` at another head (an older copy, or
  the lane's own merge commits) is refused, naming both; recovery: remove that worktree and branch (Health > Checkouts)
  once the lane holds no entry for it, then Merge again (`test_a_branch_at_another_head_is_refused_naming_both`).
- 3 Worktree: the folder. A stop after it: `resume` finds it in `git worktree list` and keeps it
  (`test_an_enqueue_the_lane_refused_is_enqueued_again_with_the_same_item`). A folder at that path that is not
  `pr/<n>`'s worktree is refused (`test_a_folder_in_the_worktrees_place_is_refused`).
- 4 Item: one row, committed before the enqueue so sd-ship reads it. A stop after it: the next read finds it by its
  ref and reuses it (`test_an_item_filed_under_the_ref_is_reused`).
- 5 Enqueue: the lane's entry. A refusal (exit 3) keeps steps 1–4 and answers `not queued`; a second Merge resumes and
  enqueues again (`test_an_enqueue_the_lane_refused_is_enqueued_again_with_the_same_item`). Any other failure is a 502
  whose outcome is unknown (`test_an_enqueue_that_gave_no_answer_is_unknown`). A copy the lane already merged is
  refused (`test_a_copy_the_lane_merged_is_not_enqueued_again`).

Once the lane merged the copy, Close original closes the original pull request with a link to the lane's
(`pr_close`, sd:5669); until then the original stays open, and the confirm says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from . import land
from .land import Unknown

__all__ = ["preview", "request"]

#: A fetch reads a whole pull request from GitHub: longer than the local git steps' bound.
FETCH_SECONDS = 120
#: The item a pull request is filed under, as `today_github` reads it back (`REF_SOURCE`, `github:<ref>`).
REF = "github:{repo}#{number}"
OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

_git, _out, _ref = land._git, land._out, land._ref


def _ref_names(repo: str, number: int) -> list[str]:
    """The ref as filed, and lower-cased: GitHub reads an owner and a name in any case (`today_github.document`)."""
    ref = REF.format(repo=repo, number=number)
    return list(dict.fromkeys((ref, ref.lower())))


def _pull(connection, repo: str, number: int) -> dict:
    from sd_db import progress

    url = f"https://github.com/{repo}/pull/{number}".lower()
    row = next((row for row in progress.tracker_items(connection, tracker="github")
                if (row.get("url") or "").lower() == url), None)
    if row is None:
        raise ValueError(f"{repo}#{number} is not an open pull request as sd shadow sync last saw it. Nothing moved.")
    return row


def _item(connection, repo: str, number: int) -> dict | None:
    from sd_db import writes

    from .today_github import REF_SOURCE

    for ref in _ref_names(repo, number):
        held = writes.item_by_external(connection, REF_SOURCE, ref)
        if held is not None:
            if held["status"] == "done":
                raise ValueError(f"sd:{held['id']}, filed under {ref}, is done; the lane takes only an open item. "
                                 "Nothing moved.")
            return {"id": held["id"], "title": " ".join(str(held["title"]).split()), "ref": ref}
    return None


def _worktrees(path: Path) -> dict[str, str]:
    """Branch ref -> worktree path, from `git worktree list --porcelain`."""
    found, tree = {}, None
    for line in _out(path, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            tree = line[len("worktree "):]
        elif line.startswith("branch ") and tree:
            found[line[len("branch "):]] = tree
    return found


def _read(connection, repo: str, number: int) -> dict:
    """The facts a preview shows and a write checks again; refusals are `ValueError`, a git step with no answer
    `Unknown`."""
    from sd_db import paths, repos

    from .health_collectors import merged_heads
    from .today_github import _checkouts

    row = _pull(connection, repo, number)
    key = _checkouts(connection).get(f"github.com/{repo.lower()}")
    if key is None:
        raise ValueError(f"{repo} is not a registered repository, so no checkout holds it to copy. Nothing moved.")
    path = paths.disk(key)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this machine. Nothing moved.")
    # The raw setting, not `remote get-url`, which rewrites through insteadOf: the evidence is what origin names.
    origin = _git(path, "config", "--get", "remote.origin.url", check=False).stdout.strip()
    if repos.remote_identity(origin) != f"github.com/{repo.lower()}":
        raise ValueError(f"{path}'s origin is {origin or 'not set'}, not {repo}: the copy would come from another "
                         "repository. Nothing moved.")
    listed = _git(path, "ls-remote", "origin", f"refs/pull/{number}/head", seconds=FETCH_SECONDS).stdout.split()
    head = listed[0] if listed and OBJECT.fullmatch(listed[0]) else None
    if head is None:
        raise ValueError(f"origin lists no head for {repo}#{number}. Nothing moved.")
    branch = f"pr/{number}"
    tip = _ref(path, f"refs/heads/{branch}")
    trees = _worktrees(path)
    worktree = trees.get(f"refs/heads/{branch}") or str(Path.home() / "worktrees" / f"{path.name}-pr-{number}")
    if tip and (os.path.realpath(worktree), tip) in merged_heads(connection):
        raise ValueError(f"The lane already merged {tip[:12]} from {worktree}: remove {branch} (Health > Checkouts) "
                         "before Merge copies it again. Nothing moved.")
    if tip and tip != head:
        raise ValueError(f"{branch} holds {tip[:12]}, but {repo}#{number}'s head is {head[:12]}: an earlier copy, or "
                         f"the lane's work on it. Once the lane holds no entry for it, remove {worktree} and {branch} "
                         "(Health > Checkouts), then Merge again. Nothing moved.")
    listed_tree = f"refs/heads/{branch}" in trees
    if not listed_tree and (os.path.lexists(worktree)):
        raise ValueError(f"{worktree} exists and is not {branch}'s worktree. Move it aside, then Merge again. "
                         "Nothing moved.")
    item = _item(connection, repo, number)
    title = " ".join(str(row.get("title") or "").split()) or f"{repo}#{number}"
    return {"repo": repo, "number": number, "url": f"https://github.com/{repo}/pull/{number}", "title": title,
            "author": row.get("author"), "checkout": key, "disk": str(path), "head": head, "branch": branch,
            "tip": tip, "worktree": worktree, "worktree_listed": listed_tree, "mode": "resume" if tip else "copy",
            "item": item["id"] if item else None, "ref": item["ref"] if item else _ref_names(repo, number)[0],
            "item_title": item["title"] if item else f"Merge {repo}#{number}: {title}"}


def revision(facts: dict) -> str:
    """What the preview showed and the write must find again: the pull request, its head, the copy and the item."""
    subject = [facts["repo"].lower(), facts["number"], facts["checkout"], facts["head"], facts["author"], facts["title"],
               facts["tip"], facts["worktree"], facts["worktree_listed"], facts["item"], facts["item_title"]]
    return hashlib.sha256(json.dumps(subject, sort_keys=True).encode()).hexdigest()


def _title(facts: dict) -> str:
    return f"{facts['title']} ({facts['repo']}#{facts['number']})"


def _body(facts: dict, item: int) -> str:
    author = f"by {facts['author']}" if facts["author"] else "(author not recorded)"
    return "\n".join([
        "## Summary", "",
        f"- sd:{item}: {facts['item_title']}.",
        f"- Copies {facts['url']} {author} at {facts['head'][:12]}; its commits and their authors are unchanged.", "",
        "## Test plan", "",
        "- Copied with the Today GitHub view's Merge action; the lane's local gate runs `make check` before the merge.",
        ""])


def _argv(facts: dict, item, head: str, body: str) -> list[str]:
    from .queue_screen import command

    return [command("sd-ship"), "-C", facts["worktree"], "lane", "enqueue", "--item", str(item), "--title",
            _title(facts), "--body-file", body, "--expected-head", head, "--deliver"]


def _cli(facts: dict) -> str:
    fetch = "" if facts["mode"] == "resume" else (
        f"git -C {shlex.quote(facts['disk'])} fetch origin pull/{facts['number']}/head && ")
    argv = _argv(facts, facts["item"] or "<new item>", facts["head"], "<body file>")
    return fetch + " ".join(["sd-ship", *map(shlex.quote, argv[1:])])


def _values(repo, number, action: str = "Merge") -> tuple[str, int]:
    from .today_github import SLUG

    if isinstance(number, str) and re.fullmatch(r"[1-9][0-9]{0,7}", number):
        number = int(number)
    if (not isinstance(repo, str) or not SLUG.fullmatch(repo) or {".", ".."} & set(repo.split("/"))
            or type(number) is not int or not 1 <= number <= 10_000_000):
        raise ValueError(f"{action} takes a pull request's repo, as owner/name, and its number.")
    return repo, number


def preview(connection, parameters: dict) -> dict | tuple[int, dict]:
    """`GET /api/today/github/merge/preview?repo=<owner/name>&number=<n>`: what Merge would do, and its revision."""
    first = {name: (value[0] if isinstance(value, list) and value else value) for name, value in parameters.items()}
    try:
        repo, number = _values(first.get("repo"), first.get("number"))
        facts = _read(connection, repo, number)
    except ValueError as error:
        return 409, {"error": str(error)}
    except Unknown as error:
        return 502, {"error": f"The pull request was not read: {error}"}
    return {**facts, "revision": revision(facts), "cli": _cli(facts)}


def merge(connection, repo: str, number: int, expected: str, who: str):
    facts = _read(connection, repo, number)
    if revision(facts) != expected:
        from sd_db import workflow

        raise workflow.StaleItem("The pull request or its copy changed since the preview. Nothing moved. "
                                 "Open Merge again.")
    path, head = Path(facts["disk"]), facts["head"]
    done: dict = {"repo": repo, "number": number, "branch": facts["branch"], "worktree": facts["worktree"],
                  "head": head, "mode": facts["mode"], "item": facts["item"], "filed": False}
    try:
        if not facts["tip"]:
            _git(path, "fetch", "-q", "origin", f"pull/{number}/head", seconds=FETCH_SECONDS)
            fetched = _out(path, "rev-parse", "FETCH_HEAD")
            if fetched != head:
                from sd_db import workflow

                raise workflow.StaleItem(f"{repo}#{number}'s head moved to {fetched[:12]} during the copy; only its "
                                         "objects were fetched. Open Merge again.")
            _git(path, "branch", facts["branch"], head)
        if not facts["worktree_listed"]:
            tree = Path(facts["worktree"])
            tree.parent.mkdir(parents=True, exist_ok=True)
            _git(path, "worktree", "add", "-q", str(tree), facts["branch"])
        if facts["item"] is None:
            from .server import capture_ref

            filed = capture_ref(connection, ref=facts["ref"], who=who, title=facts["item_title"], repo=facts["checkout"],
                                body=f"Copied from {facts['url']} by the Today GitHub view's Merge; the lane merges "
                                     f"its own pull request from {facts['worktree']}.")
            done.update(item=filed["item"]["id"], filed=True)
        return land.enqueue(Path(facts["worktree"]), "sd-merge-body.md", _body(facts, done["item"]),
                            lambda body: _argv(facts, done["item"], head, body), head, done, "Copied")
    except Unknown as error:
        return 502, {"error": f"Merge stopped part way: {error}", "outcome": "unknown", **done}


def request(payload: dict, principal: str):
    """`POST /api/today/github/merge`: the page's checks before the server opens a writer; the write as `action_route`
    takes it."""
    values = dict(payload)
    if set(values) != {"repo", "number", "revision"} or not isinstance(values["revision"], str) \
            or not re.fullmatch(r"[0-9a-f]{64}", values["revision"]) or isinstance(values["number"], str):
        raise ValueError("Merge takes the pull request's repo, its number and the preview's revision.")
    repo, number = _values(values["repo"], values["number"])
    return lambda connection: merge(connection, repo, number, values["revision"], principal)
